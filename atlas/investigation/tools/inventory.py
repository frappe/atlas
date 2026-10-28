from __future__ import annotations

import json
from typing import Annotated

import frappe
from frappe import _
from frappe.flow import ToolContext
from frappe.utils import add_to_date, now_datetime

from atlas.investigation.core.telemetry import TelemetryQuery
from atlas.investigation.tools.decorators import read_tool

HOST_FIELDS = (
	"name", "title", "status", "is_provisioning_completed", "is_in_maintenance", "is_sleepy_vm_host",
	"server_size", "architecture", "public_ipv4_address", "private_ipv4_address", "wireguard_ip_address",
	"bmc_power_state", "metald_tls_expires_on",
)  # fmt: skip

VIRTUAL_MACHINE_FIELDS = (
	"name", "tenant_id", "server", "hostname", "current_state", "desired_state", "error", "is_terminating",
	"active_migration", "is_privileged", "is_network_gateway", "is_termination_protected", "architecture",
	"virtual_machine_image", "cpu_millicores", "memory_mib", "disk_mib", "sleep_after_idle_seconds",
	"public_ipv4", "public_ipv6", "routed_ipv6", "wireguard_mesh_ipv6",
)  # fmt: skip
RECENT_LIMIT = 10


@read_tool()
def find_resources(
	text: Annotated[str, "A name, title, host name, IP address, or tenant ID."], *, ctx: ToolContext
) -> dict:
	"""Find hosts (Metal Servers) and VMs that match a name, title, host name, IP address, or tenant ID."""
	pattern = f"%{text.strip()}%"
	hosts = frappe.get_all(
		"Metal Server",
		or_filters={
			field: ("like", pattern)
			for field in (
				"name",
				"title",
				"public_ipv4_address",
				"private_ipv4_address",
				"wireguard_ip_address",
			)
		},
		fields=["name", "title", "status"],
		limit=RECENT_LIMIT,
	)
	virtual_machine_filters = {
		field: ("like", pattern) for field in ("name", "hostname", "public_ipv4", "wireguard_mesh_ipv6")
	}
	if text.strip().isdigit():
		virtual_machine_filters["tenant_id"] = int(text)
	virtual_machines = frappe.get_all(
		"Virtual Machine",
		or_filters=virtual_machine_filters,
		fields=["name", "tenant_id", "server", "hostname"],
		limit=RECENT_LIMIT * 5,
	)
	return {"hosts": hosts, "virtual_machines": virtual_machines}


@read_tool()
def describe_host(host: Annotated[str, "Metal Server name or title."], *, ctx: ToolContext) -> dict:
	"""Read one host: its state, its VMs by state, and its recent console runs and drains."""
	server = get_host_name(host)
	document = frappe.get_doc("Metal Server", server)
	virtual_machines = [
		frappe.get_doc("Virtual Machine", name)
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name")
	]
	return {
		"host": {field: document.get(field) for field in HOST_FIELDS},
		"virtual_machines": [
			{"name": vm.name, "tenant_id": vm.tenant_id, "current_state": vm.current_state, "error": vm.error}
			for vm in virtual_machines
		],
		"recent_console_logs": frappe.get_all(
			"Metal Server Console Log",
			filters={"servers": ("like", f"%{server}%")},
			fields=["name", "status", "owner", "creation", "script"],
			order_by="creation desc",
			limit=RECENT_LIMIT,
		),
		"recent_ssh_tasks": _recent_ssh_tasks("Metal Server", server),
		"recent_drains": frappe.get_all(
			"Metal Server Drain",
			filters={"source_metal_server": server},
			fields=["name", "status", "creation", "modified"],
			order_by="creation desc",
			limit=RECENT_LIMIT,
		),
	}


@read_tool()
def describe_virtual_machine(name: Annotated[str, "Virtual Machine name."], *, ctx: ToolContext) -> dict:
	"""Read one VM: its state, placement, network, and recent migrations."""
	document = frappe.get_doc("Virtual Machine", name)
	return {
		"virtual_machine": {field: getattr(document, field) for field in VIRTUAL_MACHINE_FIELDS},
		"host_title": frappe.db.get_value("Metal Server", document.server, "title"),
		"recent_migrations": frappe.get_all(
			"Virtual Machine Migration",
			filters={"virtual_machine": name},
			fields=[
				"name",
				"status",
				"source_metal_server",
				"destination_metal_server",
				"started_at",
				"finished_at",
				"error_code",
				"error_message",
			],
			order_by="creation desc",
			limit=RECENT_LIMIT,
		),
		"recent_ssh_tasks": _recent_ssh_tasks("Virtual Machine", name),
	}


@read_tool()
def list_recent_errors(
	since_minutes: Annotated[int, "How far back to look, in minutes."] = 60, *, ctx: ToolContext
) -> list[dict]:
	"""Count the Atlas Error Logs by method since a time. Read one with read_error_log."""
	since = add_to_date(now_datetime(), minutes=-since_minutes)
	return frappe.db.sql(
		"""SELECT method, COUNT(*) AS count, MAX(creation) AS last_seen, MAX(name) AS last_error_log
		FROM `tabError Log` WHERE creation > %s GROUP BY method ORDER BY count DESC LIMIT 30""",
		since,
		as_dict=True,
	)


@read_tool()
def read_error_log(name: Annotated[str, "Error Log name."], *, ctx: ToolContext) -> dict:
	"""Read the traceback and reference of one Atlas Error Log."""
	error_log = frappe.get_doc("Error Log", name)
	return {
		"method": error_log.method,
		"creation": error_log.creation,
		"reference": [error_log.reference_doctype, error_log.reference_name],
		"error": (error_log.error or "")[-8_000:],
	}


@read_tool(background=True)
def get_host_changes(
	host: Annotated[str, "Metal Server name or title."],
	hours: Annotated[int, "How far back to look, in hours."] = 24,
	*,
	ctx: ToolContext,
) -> dict:
	"""Return what changed on a host: metald and kernel versions, metald restarts, reboots, field changes
	such as maintenance, SSH tasks, drains, and Metald Updates."""
	server = get_host_name(host)
	since = add_to_date(now_datetime(), hours=-hours)
	return {
		"reported_by_host": TelemetryQuery().get_changes(server, hours).as_dict(),
		"field_changes": [
			{
				"changed_at": version.creation,
				"by": version.owner,
				"changed": json.loads(version.data).get("changed"),
			}
			for version in frappe.get_all(
				"Version",
				filters={"ref_doctype": "Metal Server", "docname": server, "creation": (">", since)},
				fields=["creation", "owner", "data"],
				order_by="creation desc",
				limit=RECENT_LIMIT,
			)
		],
		"ssh_tasks": frappe.get_all(
			"SSH Task",
			filters={"target_type": "Metal Server", "target": server, "creation": (">", since)},
			fields=["name", "status", "script_path", "exit_code", "creation"],
			order_by="creation desc",
			limit=RECENT_LIMIT * 3,
		),
		"drains": frappe.get_all(
			"Metal Server Drain",
			filters={"source_metal_server": server, "modified": (">", since)},
			fields=["name", "status", "creation", "modified"],
		),
		"metald_updates": frappe.get_all(
			"Metald Update Server",
			filters={"server": server, "modified": (">", since)},
			fields=["parent", "status", "metald_version", "status_reason", "modified"],
		),
	}


def get_host_name(host: str) -> str:
	"""Return the Metal Server name for a name or a title."""
	name = frappe.db.get_value("Metal Server", {"name": host}) or frappe.db.get_value(
		"Metal Server", {"title": host}
	)
	if not name:
		frappe.throw(_("No Metal Server has the name or title {0}.").format(host))
	return name


def _recent_ssh_tasks(target_type: str, target: str) -> list[dict]:
	return frappe.get_all(
		"SSH Task",
		filters={"target_type": target_type, "target": target},
		fields=["name", "status", "script_path", "exit_code", "creation"],
		order_by="creation desc",
		limit=RECENT_LIMIT,
	)
