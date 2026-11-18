from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.ssh import SSHRunner
from atlas.atlas.core.telemetry.agent import INSTALL_TIMEOUT_SECONDS, DatumAgent
from atlas.atlas.core.telemetry.collectors import SERVICE_DOCTYPES
from atlas.atlas.core.telemetry.datum import get_datum_url
from atlas.metal_server.core.host_installation import throw_script_failure

RUNWAY_CACHE_DOCTYPE = "Runway Cache Server"
RUNWAY_CACHE_METRICS = {
	"metrics_listen": "127.0.0.1:18446",
	"data_metrics_listen": "127.0.0.1:18447",
}


class ServiceTelemetry:
	"""Install the Datum agent on a service VM. Cargo sends machine metrics and logs without a role file."""

	def __init__(self, node: Document) -> None:
		self.node = node
		is_cargo = node.doctype == "Cargo Server"
		self.product = "cargo" if is_cargo else "atlas"
		if is_cargo:
			self.role = None
		elif node.doctype == RUNWAY_CACHE_DOCTYPE:
			self.role = "runway_cache"
		else:
			self.role = SERVICE_DOCTYPES[node.doctype]
		self.virtual_machine = frappe.get_doc("Virtual Machine", node.virtual_machine)

	def configure(self) -> None:
		runner = SSHRunner(
			self.virtual_machine.ssh_host, proxy_command=self.virtual_machine.get_ssh_proxy_command()
		)
		result = DatumAgent(runner, self.virtual_machine.name, self.product).install(role=self.role)
		if not result.is_success:
			throw_script_failure(
				_("Could not install the Datum agent on {0}.").format(self.node.name), result
			)


def enqueue_service_telemetry(node: Document) -> None:
	"""Queue the telemetry setup of one node. A queued setup absorbs a second request."""
	if not get_datum_url():
		return

	frappe.enqueue(
		configure_service_telemetry,
		queue="long",
		timeout=INSTALL_TIMEOUT_SECONDS,
		doctype=node.doctype,
		name=node.name,
		job_id=f"atlas||service-telemetry||{node.doctype}||{node.name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def request_service_telemetry(node: Document) -> None:
	"""Queue the telemetry setup of an active node for an operator."""
	if node.status != "Active":
		frappe.throw(_("{0} is not active.").format(node.name))
	if not get_datum_url():
		frappe.throw(_("Set datum_url in the site config to send telemetry."))

	enqueue_service_telemetry(node)


def configure_service_telemetry(doctype: str, name: str) -> None:
	if not get_datum_url():
		return

	node = frappe.get_single(doctype) if doctype == RUNWAY_CACHE_DOCTYPE else frappe.get_doc(doctype, name)
	if node.status == "Active":
		ServiceTelemetry(node).configure()


def renew_service_telemetry_credentials() -> None:
	"""Queue a daily telemetry setup with a fresh token for each active service."""
	if not get_datum_url():
		return

	for doctype in SERVICE_DOCTYPES:
		for name in frappe.get_all(doctype, filters={"status": "Active"}, pluck="name"):
			enqueue_service_telemetry(frappe.get_doc(doctype, name))

	for doctype in ("Cargo Server", RUNWAY_CACHE_DOCTYPE):
		node = frappe.get_single(doctype)
		if node.status == "Active":
			enqueue_service_telemetry(node)
