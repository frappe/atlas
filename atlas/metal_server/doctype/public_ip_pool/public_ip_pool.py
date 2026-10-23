from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.tags import validate_tags
from atlas.service.core.ipv6_router.address import validate_router_network

ATLAS_TENANT_ID = 0

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


@dataclass(frozen=True, slots=True)
class ProviderIntent:
	version: int
	status: str
	provider_resource_id: str
	prefix: str
	server: str | None
	host_address: str | None
	ip_version: int


class PublicIPPool(Document):
	"""Own one public address range and its provider attachment."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.atlas.doctype.atlas_tag.atlas_tag import AtlasTag

		allocation_prefix_length: DF.Int
		attached_server: DF.Link | None
		enabled: DF.Check
		gateway: DF.Link | None
		host_address: DF.Data | None
		intent_version: DF.Int
		next_allocation_offset: DF.Data | None
		prefix: DF.Data
		provider_resource_id: DF.Data | None
		provider_status: DF.Literal["Not Applicable", "Available", "Attaching", "Attached", "Detaching"]
		source: DF.Literal["Static", "Provider"]
		tags: DF.Table[AtlasTag]
		version: DF.Literal["4", "6"]
	# end: auto-generated types

	def validate(self) -> None:
		validate_tags(self)
		network = self._canonical_network()
		self.prefix = str(network)
		self.version = str(network.version)
		self._validate_geometry(network)
		self._validate_source()
		self._validate_gateway(network)
		self._validate_overlap(network)
		self._validate_immutable_geometry()

	def on_trash(self) -> None:
		if self.gateway:
			frappe.throw(_("Archive this pool's IPv6 Router Server before deletion."))
		if self.provider_status not in {"Not Applicable", "Available"}:
			frappe.throw(_("Detach this pool from its Metal Server before deletion."))
		self._delete_available_allocations()
		if self.source == "Provider":
			frappe.get_single("Atlas Settings").server_provider_controller.delete_public_ip_address(
				self.provider_resource_id
			)

	@property
	def is_routed(self) -> bool:
		return bool(self.gateway)

	@property
	def network(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
		return ipaddress.ip_network(self.prefix)

	def begin_provider_attach(self, server: str) -> None:
		if self.source == "Static":
			return
		if self.provider_status == "Attached" and self.attached_server == server:
			return
		self.provider_status = "Attaching"
		self.attached_server = server
		self.host_address = None
		self.intent_version = (self.intent_version or 0) + 1
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def begin_provider_detach(self) -> None:
		if self.source == "Static" or self.provider_status == "Available":
			return
		self.provider_status = "Detaching"
		self.intent_version = (self.intent_version or 0) + 1
		self.save(ignore_permissions=True)
		self.queue_reconcile()

	def queue_reconcile(self) -> None:
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"reconcile",
			queue="default",
			job_id=f"atlas||public-ip-pool||reconcile||{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@frappe.whitelist(methods=["POST"])
	def generate_available_allocations(self) -> int:
		"""Create the next batch of direct allocations from this pool."""
		frappe.only_for("System Manager")
		if self.is_routed:
			frappe.throw(_("A routed pool creates an address when a VM requests IPv6."))
		if self.source == "Provider":
			frappe.throw(_("A provider pool creates an allocation when an address is requested."))
		from atlas.metal_server.core.public_ip_service import generate_available_allocations

		return generate_available_allocations(self.name)

	@frappe.whitelist(methods=["POST"])
	def reserve_allocation(self) -> str:
		"""Reserve the next allocation of this direct pool for Atlas services, which use tenant 0."""
		frappe.only_for("System Manager")
		from atlas.metal_server.core.public_ip_service import PublicIPService

		return PublicIPService().reserve_from_pool(self, ATLAS_TENANT_ID).name

	@frappe.whitelist(methods=["POST"])
	def retry_provider_operation(self) -> None:
		"""Queue the current provider operation again."""
		frappe.only_for("System Manager")
		if self.provider_status not in {"Attaching", "Detaching"}:
			frappe.throw(_("This pool has no provider operation to retry."))
		self.queue_reconcile()

	@run_as_admin
	def reconcile(self) -> None:
		current: PublicIPPool = frappe.get_doc(self.doctype, self.name)
		intent = current.provider_intent()
		if intent.status not in {"Attaching", "Detaching"}:
			return

		try:
			host_address = current._apply_provider_intent(intent)
			current._complete_provider_intent(intent, host_address)
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Public IP Pool {self.name} intent {intent.version} failed",
			)
			raise

	def provider_intent(self) -> ProviderIntent:
		return ProviderIntent(
			version=self.intent_version or 0,
			status=self.provider_status,
			provider_resource_id=self.provider_resource_id or "",
			prefix=self.prefix,
			server=self.attached_server,
			host_address=self.host_address,
			ip_version=int(self.version),
		)

	def _apply_provider_intent(self, intent: ProviderIntent) -> str | None:
		if not intent.server:
			raise ValueError("A provider intent needs a Metal Server")
		server: MetalServer = frappe.get_doc("Metal Server", intent.server)
		provider = frappe.get_single("Atlas Settings").server_provider_controller
		if intent.status == "Detaching":
			provider.detach_public_ip_address(intent.provider_resource_id, intent.host_address, server)
			return None

		public_address = str(ipaddress.ip_network(intent.prefix).network_address)
		host_address = provider.attach_public_ip_address(intent.provider_resource_id, public_address, server)
		if intent.ip_version == 6:
			return None
		try:
			return str(ipaddress.IPv4Address(host_address))
		except ipaddress.AddressValueError as error:
			raise ValueError("A provider attach must return an IPv4 host address") from error

	def _complete_provider_intent(self, intent: ProviderIntent, host_address: str | None) -> bool:
		pool = frappe.qb.DocType("Public IP Pool")
		is_attaching = intent.status == "Attaching"
		(
			frappe.qb.update(pool)
			.set(pool.provider_status, "Attached" if is_attaching else "Available")
			.set(pool.attached_server, intent.server if is_attaching else None)
			.set(pool.host_address, host_address if is_attaching else None)
			.where(pool.name == self.name)
			.where(pool.intent_version == intent.version)
		).run()
		return frappe.db.get_value(self.doctype, self.name, "provider_status") == (
			"Attached" if is_attaching else "Available"
		)

	def _canonical_network(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
		try:
			return ipaddress.ip_network(self.prefix, strict=False)
		except ValueError:
			frappe.throw(_("Prefix must be a valid IPv4 or IPv6 network."))
		raise AssertionError

	def _validate_geometry(self, network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
		if network.version == 4:
			self.allocation_prefix_length = 32
		if not network.prefixlen <= self.allocation_prefix_length <= network.max_prefixlen:
			frappe.throw(_("Allocation prefix length must fit inside the pool."))
		if self.source == "Provider" and not self.gateway:
			if self.allocation_prefix_length != network.prefixlen:
				frappe.throw(_("A direct provider pool cannot be divided into smaller allocations."))

	def _validate_source(self) -> None:
		if self.source == "Provider":
			if not self.provider_resource_id:
				frappe.throw(_("A provider pool needs a provider resource ID."))
			if not self.provider_status or self.provider_status == "Not Applicable":
				self.provider_status = "Available"
		elif self.provider_resource_id:
			frappe.throw(_("A static pool cannot have a provider resource ID."))
		else:
			self.provider_status = "Not Applicable"
			self.attached_server = None
			self.host_address = None

	def _validate_gateway(self, network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
		if not self.gateway:
			return
		if network.version != 6:
			frappe.throw(_("Only an IPv6 pool can have a gateway."))
		if self.allocation_prefix_length != 128:
			frappe.throw(_("A gateway pool must create /128 allocations."))
		validate_router_network(network)
		other = frappe.db.exists("Public IP Pool", {"gateway": self.gateway, "name": ["!=", self.name]})
		if other:
			frappe.throw(_("This IPv6 Router Server already belongs to another pool."))

	def _validate_overlap(self, network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
		for row in frappe.get_all(
			"Public IP Pool",
			filters={"version": str(network.version), "name": ["!=", self.name]},
			fields=["prefix"],
		):
			if network.overlaps(ipaddress.ip_network(row.prefix)):
				frappe.throw(_("This prefix overlaps another Public IP Pool."))

	def _validate_immutable_geometry(self) -> None:
		previous = self.get_doc_before_save()
		if not previous:
			return
		geometry_changed = (
			self.prefix != previous.prefix
			or self.allocation_prefix_length != previous.allocation_prefix_length
			or self.version != previous.version
		)
		gateway_changed = self.gateway != previous.gateway
		if (geometry_changed or gateway_changed) and frappe.db.exists(
			"Public IP Allocation", {"pool": self.name}
		):
			frappe.throw(
				_("Remove this pool's allocations before you change its address geometry or gateway.")
			)

	def _delete_available_allocations(self) -> None:
		if frappe.db.exists(
			"Public IP Allocation", {"pool": self.name, "status": ["not in", ["Available", "Blocked"]]}
		):
			frappe.throw(_("Release or detach all allocations in use before you delete this pool."))
		for name in frappe.get_all("Public IP Allocation", filters={"pool": self.name}, pluck="name"):
			frappe.delete_doc("Public IP Allocation", name, ignore_permissions=True, delete_permanently=True)


@frappe.whitelist(methods=["POST"])
def add_static_pool(prefix: str, allocation_prefix_length: int = 0) -> str:
	"""Add an operator-routed pool and generate its first allocations."""
	frappe.only_for("System Manager")
	pool: PublicIPPool = frappe.get_doc(
		{
			"doctype": "Public IP Pool",
			"prefix": prefix,
			"allocation_prefix_length": int(allocation_prefix_length),
			"enabled": 1,
			"source": "Static",
		}
	).insert()
	pool.generate_available_allocations()
	return pool.name


@frappe.whitelist(methods=["POST"])
def reserve_from_provider(version: int = 4) -> str:
	"""Reserve one provider resource and store it as a pool."""
	frappe.only_for("System Manager")
	version = int(version)
	if version not in {4, 6}:
		frappe.throw(_("Version must be 4 or 6."))
	provider = frappe.get_single("Atlas Settings").server_provider_controller
	reserved = provider.reserve_public_ip_address(version)
	try:
		network = ipaddress.ip_network(reserved.address, strict=False)
		pool: PublicIPPool = frappe.get_doc(
			{
				"doctype": "Public IP Pool",
				"prefix": str(network),
				"version": str(network.version),
				"allocation_prefix_length": network.prefixlen if network.version == 6 else 32,
				"source": "Provider",
				"provider_resource_id": reserved.provider_resource_id,
				"provider_status": "Available",
			}
		).insert()
		return pool.name
	except Exception:
		try:
			provider.delete_public_ip_address(reserved.provider_resource_id)
		except Exception:
			frappe.log_error(title="Could not delete a reserved public IP resource")
		raise


def enqueue_pending_pool_reconciliation() -> None:
	for name in frappe.get_all(
		"Public IP Pool", filters={"provider_status": ["in", ["Attaching", "Detaching"]]}, pluck="name"
	):
		frappe.get_doc("Public IP Pool", name).queue_reconcile()
