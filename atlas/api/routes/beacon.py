from __future__ import annotations

import hashlib
import json

import frappe

from atlas.api.core.base import ApiResult
from atlas.api.core.docs import api_docs
from atlas.api.models import BeaconResourceListResponse
from atlas.api.router import beacon
from atlas.vm.core.vm_state import SLEEP_STATES

# Beacon knows four resource statuses. Each Atlas status maps to one of them.
HOST_STATUSES = {
	"Pending": "Pending",
	"Installing": "Pending",
	"Running": "Active",
	"Stopped": "Suspended",
	"Failed": "Suspended",
	"Deleted": "Terminated",
}
SERVICE_STATUSES = {
	"Pending": "Pending",
	"Provisioning": "Pending",
	"Active": "Active",
	"Failed": "Suspended",
	"Archived": "Terminated",
}
SERVICE_DOCTYPES = ("Proxy Server", "Wireguard Gateway Server", "IPv6 Router Server")


@beacon.get("resources", central_only=True)
@api_docs(
	responses={
		304: {"description": "The list has not changed since the `If-None-Match` ETag."},
	}
)
def list_beacon_resources() -> ApiResult[BeaconResourceListResponse]:
	"""List resources for Beacon.

	Returns Atlas itself and every host, virtual machine, service node and the Cargo server of this region, with its parent, so Beacon can match telemetry to resources. The ETag changes only when the list changes.
	"""
	body = {"resources": BeaconResourceList().collect()}
	etag = '"' + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + '"'
	if frappe.get_request_header("If-None-Match") == etag:
		return ApiResult(None, status=304, headers={"ETag": etag})
	return ApiResult(body, headers={"ETag": etag})


class BeaconResourceList:
	"""The regional resources that Beacon monitors, in the row format of a Beacon resource source."""

	def __init__(self) -> None:
		self.rows: list[dict[str, str]] = []

	def collect(self) -> list[dict[str, str]]:
		# Atlas itself is a resource, so alerts about the control plane do not join every region incident.
		self.add("atlas", "Atlas", "Atlas", "Active")
		self.add_hosts()
		self.add_virtual_machines()
		self.add_service_nodes()
		self.add_cargo()
		return self.rows

	def add(self, resource_id: str, resource_type: str, title: str, status: str, parent_id: str = "") -> None:
		self.rows.append(
			{
				"resource_id": resource_id,
				"resource_type": resource_type,
				"parent_id": parent_id or "",
				"title": title or resource_id,
				"status": status,
			}
		)

	def add_hosts(self) -> None:
		for host in frappe.get_all("Metal Server", fields=["name", "title", "status"], order_by="name"):
			self.add(host.name, "Host", host.title, HOST_STATUSES.get(host.status, "Pending"))

	def add_virtual_machines(self) -> None:
		"""A draft is not placed yet, so it has no host. The hostname is virtual, so the title is the name.
		A sleeping VM is Suspended, so its silent Pilot does not alert. Any other stopped VM stays Active: a crashed
		VM must keep its alerts and the checks of the service node on it."""
		stopped = set(
			frappe.get_all(
				"Virtual Machine State",
				filters={"status": "stopped", "sleep_state": ("in", SLEEP_STATES)},
				pluck="virtual_machine",
			)
		)
		for machine in frappe.get_all(
			"Virtual Machine",
			fields=["name", "server", "is_draft", "is_terminating"],
			order_by="name",
		):
			self.add(
				machine.name, "Server", machine.name, get_machine_status(machine, stopped), machine.server
			)

	def add_service_nodes(self) -> None:
		for doctype in SERVICE_DOCTYPES:
			for node in frappe.get_all(
				doctype, fields=["name", "status", "virtual_machine"], order_by="name"
			):
				self.add(
					node.name,
					doctype,
					node.name,
					SERVICE_STATUSES.get(node.status, "Pending"),
					node.virtual_machine,
				)

	def add_cargo(self) -> None:
		"""A region has one Cargo cluster, so Cargo Server is a Single."""
		cargo = frappe.get_cached_doc("Cargo Server")
		if cargo.status != "Not Provisioned":
			self.add(
				"cargo",
				"Cargo Server",
				"Cargo",
				SERVICE_STATUSES.get(cargo.status, "Pending"),
				cargo.virtual_machine,
			)


def get_machine_status(machine: frappe._dict, stopped: set[str]) -> str:
	if machine.is_draft:
		return "Pending"
	if machine.is_terminating:
		return "Terminated"
	return "Suspended" if machine.name in stopped else "Active"
