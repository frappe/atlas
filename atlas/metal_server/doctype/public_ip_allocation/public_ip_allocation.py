from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.tags import validate_tags

# Frappe stores Int as NOT NULL
# and tenant 0 is a real tenant.
UNOWNED_TENANT_ID = -1


@dataclass(frozen=True, slots=True)
class AllocationIntent:
	version: int
	status: str
	pool: str
	prefix: str
	virtual_machine: str | None
	server: str | None
	is_reserved: bool


class PublicIPAllocation(Document):
	"""Own one assignable prefix and its tenant attachment intent."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.atlas.doctype.atlas_tag.atlas_tag import AtlasTag

		failure_message: DF.SmallText | None
		intent_version: DF.Int
		is_reserved: DF.Check
		pool: DF.Link
		prefix: DF.Data
		server: DF.Link | None
		status: DF.Literal["Available", "Reserved", "Attaching", "Attached", "Detaching", "Blocked"]
		tags: DF.Table[AtlasTag]
		tenant_id: DF.Int
		version: DF.Literal["4", "6"]
		virtual_machine: DF.Link | None
	# end: auto-generated types

	def before_insert(self) -> None:
		if not getattr(self.flags, "created_by_public_ip_allocator", False):
			frappe.throw(_("Create allocations through a Public IP Pool."))

	def validate(self) -> None:
		validate_tags(self)
		pool = frappe.get_doc("Public IP Pool", self.pool)
		try:
			prefix = ipaddress.ip_network(self.prefix, strict=True)
		except ValueError:
			frappe.throw(_("Allocation prefix must be a canonical IP prefix."))
		if not prefix.subnet_of(pool.network):
			frappe.throw(_("Allocation prefix must be inside its pool."))
		if prefix.prefixlen != pool.allocation_prefix_length:
			frappe.throw(_("Allocation prefix length does not match its pool."))
		self.version = pool.version
		self._validate_state(pool.is_routed)
		self._validate_virtual_machine()
		self._validate_active_owner()
		self._validate_overlap(prefix)

	def on_trash(self) -> None:
		if self.status not in {"Available", "Reserved", "Blocked"} or self.virtual_machine:
			frappe.throw(_("Detach this allocation before deletion."))

	@property
	def is_routed(self) -> bool:
		return bool(frappe.db.get_value("Public IP Pool", self.pool, "gateway"))

	@frappe.whitelist(methods=["POST"])
	def reserve(self) -> None:
		self.check_permission("write")
		if self.is_routed:
			from atlas.metal_server.core.public_ip_service import UnsupportedIPReservation

			raise UnsupportedIPReservation()
		if self.status not in {"Reserved", "Attached"}:
			frappe.throw(_("Only an owned allocation can be reserved."))
		self.is_reserved = 1
		if self.status != "Attached":
			self.status = "Reserved"
		self.save(ignore_permissions=True)

	@frappe.whitelist(methods=["POST"])
	def release_reservation(self) -> None:
		self.check_permission("write")
		from atlas.metal_server.core.public_ip_service import PublicIPAllocationInUse

		if self.status != "Reserved" or self.virtual_machine:
			raise PublicIPAllocationInUse(_("Detach this public IP before you release it."))
		self.status = "Available"
		self.tenant_id = UNOWNED_TENANT_ID
		self.is_reserved = 0
		self.save(ignore_permissions=True)

	@frappe.whitelist(methods=["POST"])
	def block(self) -> None:
		"""Keep this unused address out of every later allocation."""
		frappe.only_for("System Manager")
		self._change_unused_status("Available", "Blocked", _("Only an available public IP can be blocked."))

	@frappe.whitelist(methods=["POST"])
	def unblock(self) -> None:
		"""Let the pool allocate this address again."""
		frappe.only_for("System Manager")
		self._change_unused_status("Blocked", "Available", _("This public IP is not blocked."))

	def _change_unused_status(self, current: str, new: str, message: str) -> None:
		# The row lock makes the check safe against a concurrent claim of the same address.
		if frappe.db.get_value("Public IP Allocation", self.name, "status", for_update=True) != current:
			frappe.throw(message)
		self.status = new
		self.save(ignore_permissions=True)

	@frappe.whitelist(methods=["POST"])
	def attach_to_virtual_machine(self, virtual_machine: str) -> None:
		"""Attach this reserved direct public IP to one virtual machine."""
		self.check_permission("write")
		if self.status != "Reserved" or self.is_routed:
			frappe.throw(_("Only a reserved direct public IP can be attached by ID."))
		frappe.get_doc("Virtual Machine", virtual_machine).attach_public_ip(int(self.version), self.name)

	@frappe.whitelist(methods=["POST"])
	def detach_from_virtual_machine(self) -> None:
		"""Detach this public IP from its current virtual machine."""
		self.check_permission("write")
		if not self.virtual_machine:
			frappe.throw(_("This public IP is not attached to a virtual machine."))
		frappe.get_doc("Virtual Machine", self.virtual_machine).detach_public_ip(int(self.version))

	@frappe.whitelist(methods=["POST"])
	def retry_operation(self) -> None:
		"""Queue the current attach or detach operation again."""
		self.check_permission("write")
		if self.status not in {"Attaching", "Detaching"}:
			frappe.throw(_("This public IP has no operation to retry."))
		self.queue_reconcile()

	def begin_attach(self, virtual_machine: str, server: str, tenant_id: int) -> None:
		self.status = "Attaching"
		self.tenant_id = tenant_id
		self.virtual_machine = virtual_machine
		self.server = server
		self.failure_message = None
		self.intent_version = (self.intent_version or 0) + 1
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def begin_detach(self) -> None:
		if self.status == "Detaching":
			self.queue_reconcile()
			return
		self.status = "Detaching"
		self.failure_message = None
		self.intent_version = (self.intent_version or 0) + 1
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def move_to_server(self, server: str) -> None:
		if self.is_routed or self.server == server:
			return
		self.status = "Attaching"
		self.server = server
		self.failure_message = None
		self.intent_version = (self.intent_version or 0) + 1
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def intent(self) -> AllocationIntent:
		return AllocationIntent(
			version=self.intent_version or 0,
			status=self.status,
			pool=self.pool,
			prefix=self.prefix,
			virtual_machine=self.virtual_machine,
			server=self.server,
			is_reserved=bool(self.is_reserved),
		)

	def queue_reconcile(self) -> None:
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"reconcile",
			queue="default",
			job_id=f"atlas||public-ip-allocation||reconcile||{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@run_as_admin
	def reconcile(self) -> None:
		from atlas.metal_server.core.public_ip_service import PublicIPService

		current: PublicIPAllocation = frappe.get_doc(self.doctype, self.name)
		intent = current.intent()
		if intent.status not in {"Attaching", "Detaching"}:
			return
		try:
			PublicIPService().apply_intent(current, intent)
		except Exception as error:
			frappe.db.set_value(
				self.doctype,
				self.name,
				"failure_message",
				str(error)[:140],
				update_modified=False,
			)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Public IP Allocation {self.name} intent {intent.version} failed",
			)
			raise

	def _validate_state(self, is_routed: bool) -> None:
		owned = self.tenant_id != UNOWNED_TENANT_ID
		if self.status in {"Available", "Blocked"} and (owned or self.virtual_machine or self.is_reserved):
			frappe.throw(_("An available or blocked allocation cannot have an owner or attachment."))
		if self.status == "Reserved" and (not owned or self.virtual_machine or not self.is_reserved):
			frappe.throw(_("A reserved allocation needs an owner and no virtual machine."))
		if self.status in {"Attaching", "Attached", "Detaching"}:
			if not owned or not self.virtual_machine or not self.server:
				frappe.throw(_("An attachment intent needs a tenant, virtual machine, and Metal Server."))
		if is_routed and self.is_reserved:
			from atlas.metal_server.core.public_ip_service import UnsupportedIPReservation

			raise UnsupportedIPReservation()

	def _validate_overlap(self, prefix: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
		for row in frappe.get_all(
			"Public IP Allocation", filters={"pool": self.pool, "name": ["!=", self.name]}, fields=["prefix"]
		):
			if prefix.overlaps(ipaddress.ip_network(row.prefix)):
				frappe.throw(_("This prefix overlaps another allocation in the pool."))

	def _validate_virtual_machine(self) -> None:
		if not self.virtual_machine:
			return
		vm_tenant = frappe.db.get_value("Virtual Machine", self.virtual_machine, "tenant_id")
		if vm_tenant != self.tenant_id:
			frappe.throw(_("The allocation and virtual machine must belong to the same tenant."))
		other = frappe.db.exists(
			"Public IP Allocation",
			{
				"virtual_machine": self.virtual_machine,
				"version": self.version,
				"name": ["!=", self.name],
			},
		)
		if other:
			frappe.throw(_("A virtual machine can have only one allocation of each IP version."))

	def _validate_active_owner(self) -> None:
		previous = self.get_doc_before_save()
		if not previous or previous.status not in {"Attaching", "Detaching"}:
			return
		if self.tenant_id != previous.tenant_id:
			frappe.throw(_("The tenant cannot change while an attachment intent is active."))


def enqueue_pending_allocation_reconciliation() -> None:
	for name in frappe.get_all(
		"Public IP Allocation", filters={"status": ["in", ["Attaching", "Detaching"]]}, pluck="name"
	):
		frappe.get_doc("Public IP Allocation", name).queue_reconcile()


def enqueue_allocation_moves() -> None:
	allocation = frappe.qb.DocType("Public IP Allocation")
	pool = frappe.qb.DocType("Public IP Pool")
	virtual_machine = frappe.qb.DocType("Virtual Machine")
	rows = (
		frappe.qb.from_(allocation)
		.join(pool)
		.on(allocation.pool == pool.name)
		.join(virtual_machine)
		.on(allocation.virtual_machine == virtual_machine.name)
		.select(allocation.name, virtual_machine.server)
		.where(allocation.status == "Attached")
		.where(allocation.server != virtual_machine.server)
		.where(pool.gateway.isnull())
	).run()
	for name, server in rows:
		frappe.enqueue_doc(
			"Public IP Allocation",
			name,
			"move_to_server",
			server=server,
			job_id=f"atlas||public-ip-allocation||move||{name}",
			deduplicate=True,
		)


def on_doctype_update() -> None:
	"""Index the attachment lookups, pool capacity checks, and scheduled status scans."""
	frappe.db.add_index("Public IP Allocation", ["virtual_machine", "version"], "virtual_machine_version")
	frappe.db.add_index("Public IP Allocation", ["pool", "status"], "pool_status")
	frappe.db.add_index("Public IP Allocation", ["status"], "status")
