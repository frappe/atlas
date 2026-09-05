from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.model.document import Document


@dataclass(frozen=True, slots=True)
class IPAddressIntent:
	"""Store one provider operation and its Atlas intent version."""

	version: int
	status: str
	provider_resource_id: str
	server: str | None


class ServerIPAddress(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		address: DF.Data
		intent_version: DF.Int
		provider_resource_id: DF.Data
		server: DF.Link | None
		status: DF.Literal["Allocated", "Attaching", "Attached", "Detaching"]
		virtual_machine: DF.Link | None
	# end: auto-generated types

	def validate(self) -> None:
		try:
			address = ipaddress.ip_interface(self.address)
		except ValueError:
			frappe.throw(_("IPv4 Address must be a valid /32 address."))
			return

		if not isinstance(address, ipaddress.IPv4Interface) or address.network.prefixlen != 32:
			frappe.throw(_("IPv4 Address must be a valid /32 address."))
		self.address = str(address.ip)

	def on_trash(self) -> None:
		if self.status != "Allocated":
			frappe.throw(_("Detach this IP address before deletion."))
		frappe.get_single("Atlas Settings").server_provider_controller.delete_ip(self.provider_resource_id)

	def begin_assignment(self, server: str, virtual_machine: str) -> None:
		"""Set an attach intent for this address."""
		if self.status != "Allocated":
			frappe.throw(_("Server IP Address {0} is not available.").format(self.name))

		self.status = "Attaching"
		self.server = server
		self.virtual_machine = virtual_machine
		self.intent_version = (self.intent_version or 0) + 1
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def release(self) -> None:
		"""Set a detach intent for this address."""
		self.virtual_machine = None
		self.intent_version = (self.intent_version or 0) + 1

		if self.server:
			self.status = "Detaching"
		else:
			self.status = "Allocated"
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def queue_reconcile(self) -> None:
		"""Queue the provider reconcile job for this address."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"reconcile",
			queue="default",
			job_id=f"reconcile-server-ip-{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def reconcile(self) -> None:
		"""Apply the current provider intent."""
		current = frappe.get_doc(self.doctype, self.name)
		intent = current.get_intent()
		if intent.status not in {"Attaching", "Detaching"}:
			return

		self.apply_intent(intent)
		self.complete_intent(intent)

	def get_intent(self) -> IPAddressIntent:
		return IPAddressIntent(
			version=self.intent_version or 0,
			status=self.status,
			provider_resource_id=self.provider_resource_id,
			server=self.server,
		)

	def apply_intent(self, intent: IPAddressIntent) -> None:
		provider = frappe.get_single("Atlas Settings").server_provider_controller
		if intent.status == "Attaching":
			if not intent.server:
				raise ValueError("An attach intent needs a Server")
			provider.attach_ip(intent.provider_resource_id, frappe.get_doc("Server", intent.server))
		else:
			provider.detach_ip(intent.provider_resource_id)

	def complete_intent(self, intent: IPAddressIntent) -> None:
		"""Complete an intent only when no newer intent exists."""
		table = frappe.qb.DocType("Server IP Address")
		status = "Attached" if intent.status == "Attaching" else "Allocated"
		server = intent.server if intent.status == "Attaching" else None

		(
			frappe.qb.update(table)
			.set(table.status, status)
			.set(table.server, server)
			.where(table.name == self.name)
			.where(table.intent_version == intent.version)
		).run()


def enqueue_pending_ip_address_reconcilation() -> None:
	"""Queue a reconcile job for each pending intent."""
	for name in frappe.get_all(
		"Server IP Address",
		filters={"status": ["in", ["Attaching", "Detaching"]]},
		pluck="name",
	):
		frappe.get_doc("Server IP Address", name).queue_reconcile()


@frappe.whitelist(methods=["POST"])
def reserve() -> str:
	"""Reserve one public IPv4 address from the provider."""
	frappe.only_for("System Manager")
	provider = frappe.get_single("Atlas Settings").server_provider_controller
	reserved = provider.reserve_ip()
	try:
		return (
			frappe.get_doc(
				{
					"doctype": "Server IP Address",
					"address": reserved.address,
					"provider_resource_id": reserved.provider_resource_id,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	except Exception:
		try:
			provider.delete_ip(reserved.provider_resource_id)
		except Exception:
			frappe.log_error(title="Could not delete reserved Server IP Address")
		raise
