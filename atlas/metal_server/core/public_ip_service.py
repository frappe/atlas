from __future__ import annotations

import ipaddress
import random
from typing import TYPE_CHECKING, cast
from uuid import UUID

import frappe
from frappe import _

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.metal_server.doctype.public_ip_allocation.public_ip_allocation import UNOWNED_TENANT_ID
from atlas.service.core.ipv6_router.address import get_routed_ipv6
from atlas.vm.core.models import (
	IPV4_INTERNET_DESTINATION,
	IPV6_INTERNET_DESTINATION,
	ROUTE_VIA_HOST,
	Route,
)

if TYPE_CHECKING:
	from atlas.metal_server.doctype.public_ip_allocation.public_ip_allocation import (
		AllocationIntent,
		PublicIPAllocation,
	)
	from atlas.metal_server.doctype.public_ip_pool.public_ip_pool import PublicIPPool
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

AUTO_ALLOCATION = "auto"
ALLOCATION_BATCH_SIZE = 1_000
ALLOCATION_REPLENISH_THRESHOLD = ALLOCATION_BATCH_SIZE // 5


class PublicIPError(AtlasUserError):
	http_status_code = 409


class PublicIPPoolEmpty(PublicIPError):
	code = "public_ip_pool_empty"


class PublicIPAlreadyAttached(PublicIPError):
	code = "public_ip_already_attached"


class PublicIPAllocationInUse(PublicIPError):
	code = "public_ip_allocation_in_use"


class PublicIPIntentInProgress(PublicIPError):
	code = "public_ip_intent_in_progress"


class IPv4InternetAccessRequired(PublicIPError):
	code = "ipv4_internet_access_required"


class UnsupportedIPReservation(AtlasUserError):
	code = "unsupported_ip_reservation"
	http_status_code = 400

	def __init__(self) -> None:
		super().__init__(_("Atlas cannot reserve a routed IPv6 address. Attach IPv6 with auto instead."))


class InvalidIPSelector(AtlasUserError):
	code = "invalid_ip_selector"
	http_status_code = 400


class PublicIPNotFound(AtlasUserError):
	code = "public_ip_not_found"
	http_status_code = 404


class PublicIPService:
	"""Allocate public prefixes and reconcile their VM network state."""

	def reserve(self, tenant_id: int, version: int) -> PublicIPAllocation:
		return self.reserve_from_pool(self._select_direct_pool(version), tenant_id)

	def reserve_from_pool(self, pool: PublicIPPool, tenant_id: int) -> PublicIPAllocation:
		if pool.is_routed:
			raise UnsupportedIPReservation()
		allocation = self._claim_direct_allocation(pool, tenant_id, is_reserved=True)
		allocation.save(ignore_permissions=True)
		return allocation

	def attach(self, virtual_machine: VirtualMachine, version: int, selector: str) -> PublicIPAllocation:
		is_creation = bool(virtual_machine.is_draft)
		virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine.name, for_update=True)
		if not is_creation:
			virtual_machine.ensure_not_migrating()
			virtual_machine.validate_network_change()
			if version == 4 and not self._has_ipv4_internet_access(virtual_machine):
				raise IPv4InternetAccessRequired(
					_("Turn on IPv4 internet access before you attach a public IPv4 address.")
				)
		if selector != AUTO_ALLOCATION:
			try:
				UUID(selector)
			except (TypeError, ValueError) as error:
				raise InvalidIPSelector(_("Public IP must be auto or a reserved public IP UUID.")) from error
		existing = self.allocation_for_vm(cast(str, virtual_machine.name), version)
		if existing:
			if selector == existing.name or selector == AUTO_ALLOCATION:
				if existing.status == "Detaching":
					raise PublicIPIntentInProgress(_("This public IP address is detaching."))
				return existing
			raise PublicIPAlreadyAttached(
				_("Detach the current public IPv{0} before you attach another one.").format(version)
			)

		if selector == AUTO_ALLOCATION:
			allocation = self._automatic_allocation(virtual_machine, version)
			locked: PublicIPAllocation = frappe.get_doc(
				"Public IP Allocation", allocation.name, for_update=True
			)
		else:
			locked = self._owned_reserved_allocation(selector, virtual_machine.tenant_id, version)
		if locked.status not in {"Available", "Reserved"} or locked.virtual_machine:
			raise PublicIPAllocationInUse(_("This public IP is not available."))
		locked.begin_attach(
			cast(str, virtual_machine.name), cast(str, virtual_machine.server), virtual_machine.tenant_id
		)
		return locked

	def detach(self, virtual_machine: VirtualMachine, version: int) -> PublicIPAllocation | None:
		virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine.name, for_update=True)
		virtual_machine.ensure_not_migrating()
		virtual_machine.validate_network_change()
		allocation = self.allocation_for_vm(cast(str, virtual_machine.name), version)
		if not allocation:
			return None
		allocation.begin_detach()
		return allocation

	def _has_ipv4_internet_access(self, virtual_machine: VirtualMachine) -> bool:
		from atlas.vm.core.vm_service import VirtualMachineService

		return (
			Route(IPV4_INTERNET_DESTINATION, ROUTE_VIA_HOST)
			in VirtualMachineService(virtual_machine).get_routes()
		)

	def allocation_for_vm(self, virtual_machine: str, version: int) -> PublicIPAllocation | None:
		name = frappe.db.get_value(
			"Public IP Allocation", {"virtual_machine": virtual_machine, "version": str(version)}
		)
		return frappe.get_doc("Public IP Allocation", name) if name else None

	def apply_intent(self, allocation: PublicIPAllocation, intent: AllocationIntent) -> None:
		from atlas.vm.core.vm_service import VirtualMachineService

		if not intent.virtual_machine or not intent.server:
			raise ValueError("An allocation intent needs a VM and Metal Server")
		pool: PublicIPPool = frappe.get_doc("Public IP Pool", intent.pool)
		virtual_machine: VirtualMachine = frappe.get_doc("Virtual Machine", intent.virtual_machine)
		if intent.status == "Attaching":
			if virtual_machine.is_draft and not VirtualMachineService(virtual_machine).get_information():
				# The create request is still in flight. The pending reconciliation retries.
				return
			self._apply_attach(pool, virtual_machine, intent.prefix, intent.server)
			self._complete_attach(allocation.name, intent.version)
			return

		self._apply_detach(pool, virtual_machine)
		if pool.source == "Provider" and not pool.is_routed:
			pool.begin_provider_detach()
			pool.reconcile()
		self._complete_detach(allocation.name, intent.version, pool.is_routed, intent.is_reserved)

	def _automatic_allocation(self, virtual_machine: VirtualMachine, version: int) -> PublicIPAllocation:
		if version == 6 and frappe.get_single("Atlas Settings").use_ipv6_router_for_auto_assignment:
			pool = self._select_routed_pool(virtual_machine)
			return self._create_routed_allocation(pool, virtual_machine)
		pool = self._select_direct_pool(version)
		return self._claim_direct_allocation(pool, virtual_machine.tenant_id, is_reserved=False)

	def _select_direct_pool(self, version: int) -> PublicIPPool:
		return self._random_pool(
			frappe.get_all(
				"Public IP Pool",
				filters={"version": str(version), "enabled": 1, "gateway": ["is", "not set"]},
				pluck="name",
			)
		)

	def _select_routed_pool(self, virtual_machine: VirtualMachine) -> PublicIPPool:
		pool_names = frappe.get_all(
			"Public IP Pool",
			filters={"version": "6", "enabled": 1, "gateway": ["is", "set"]},
			pluck="name",
		)
		active: list[PublicIPPool] = []
		local: list[PublicIPPool] = []
		for name in pool_names:
			pool: PublicIPPool = frappe.get_doc("Public IP Pool", name)
			router = frappe.db.get_value(
				"IPv6 Router Server", pool.gateway, ["status", "server"], as_dict=True
			)
			if not router or router.status != "Active":
				continue
			active.append(pool)
			if router.server == virtual_machine.server:
				local.append(pool)
		if local:
			return random.choice(local)
		if active:
			return random.choice(active)
		raise PublicIPPoolEmpty(_("No active routed IPv6 pool has capacity."))

	def _random_pool(self, pool_names: list[str]) -> PublicIPPool:
		random.shuffle(pool_names)
		for name in pool_names:
			pool: PublicIPPool = frappe.get_doc("Public IP Pool", name)
			if self._pool_has_capacity(pool):
				return pool
		raise PublicIPPoolEmpty(_("No compatible public IP pool has capacity."))

	def _pool_has_capacity(self, pool: PublicIPPool) -> bool:
		if frappe.db.exists("Public IP Allocation", {"pool": pool.name, "status": "Available"}):
			return True
		return int(pool.next_allocation_offset or 0) < pool.network.num_addresses // (
			1 << (pool.network.max_prefixlen - pool.allocation_prefix_length)
		)

	def _claim_direct_allocation(
		self, pool: PublicIPPool, tenant_id: int, *, is_reserved: bool
	) -> PublicIPAllocation:
		pool = frappe.get_doc("Public IP Pool", pool.name, for_update=True)
		name = frappe.db.get_value(
			"Public IP Allocation", {"pool": pool.name, "status": "Available"}, "name", for_update=True
		)
		allocation = frappe.get_doc("Public IP Allocation", name) if name else self._create_next(pool)
		allocation.tenant_id = tenant_id
		allocation.is_reserved = int(is_reserved)
		allocation.status = "Reserved" if is_reserved else "Available"
		return allocation

	def _create_next(self, pool: PublicIPPool) -> PublicIPAllocation:
		offset = int(pool.next_allocation_offset or 0)
		allocation_size = 1 << (pool.network.max_prefixlen - pool.allocation_prefix_length)
		if offset >= pool.network.num_addresses // allocation_size:
			raise PublicIPPoolEmpty(_("Public IP Pool {0} is empty.").format(pool.name))
		address = int(pool.network.network_address) + offset * allocation_size
		prefix = ipaddress.ip_network((address, pool.allocation_prefix_length))
		allocation: PublicIPAllocation = frappe.get_doc(
			{
				"doctype": "Public IP Allocation",
				"pool": pool.name,
				"prefix": str(prefix),
				"version": pool.version,
				"status": "Available",
				"tenant_id": UNOWNED_TENANT_ID,
			}
		)
		allocation.flags.created_by_public_ip_allocator = True
		allocation.insert(ignore_permissions=True)
		pool.db_set("next_allocation_offset", str(offset + 1), update_modified=False)
		pool.next_allocation_offset = str(offset + 1)
		return allocation

	def _create_routed_allocation(
		self, pool: PublicIPPool, virtual_machine: VirtualMachine
	) -> PublicIPAllocation:
		prefix = f"{get_routed_ipv6(pool.prefix, get_virtual_machine_mesh_address(virtual_machine))}/128"
		if frappe.db.exists("Public IP Allocation", {"prefix": prefix}):
			raise RuntimeError(f"Routed public IPv6 collision for {prefix}")
		allocation: PublicIPAllocation = frappe.get_doc(
			{
				"doctype": "Public IP Allocation",
				"pool": pool.name,
				"prefix": prefix,
				"version": "6",
				"status": "Available",
				"tenant_id": UNOWNED_TENANT_ID,
			}
		)
		allocation.flags.created_by_public_ip_allocator = True
		allocation.insert(ignore_permissions=True)
		return allocation

	def _owned_reserved_allocation(self, name: str, tenant_id: int, version: int) -> PublicIPAllocation:
		try:
			allocation: PublicIPAllocation = frappe.get_doc("Public IP Allocation", name, for_update=True)
		except frappe.DoesNotExistError as error:
			raise PublicIPNotFound(_("The public IP does not exist.")) from error
		if (
			allocation.tenant_id != tenant_id
			or allocation.version != str(version)
			or not allocation.is_reserved
			or allocation.is_routed
		):
			raise PublicIPNotFound(_("The public IP does not exist."))
		return allocation

	def _apply_attach(
		self, pool: PublicIPPool, virtual_machine: VirtualMachine, prefix: str, server: str
	) -> None:
		from atlas.vm.core.vm_service import VirtualMachineService

		service = VirtualMachineService(virtual_machine)
		service.lock_network()
		if pool.is_routed:
			router = frappe.get_doc("IPv6 Router Server", pool.gateway)
			route = Route(IPV6_INTERNET_DESTINATION, router.wireguard_mesh_ipv6)
			service.update_network({"routes": service.get_routes_with(route)})
			return

		if pool.source == "Provider":
			self._move_provider_pool(pool, server)
			pool.begin_provider_attach(server)
			pool.reconcile()
			pool.reload()
		# Replies from a public address leave through the host uplink.
		address = str(ipaddress.ip_network(prefix).network_address)
		if pool.version == "4":
			route = Route(IPV4_INTERNET_DESTINATION, ROUTE_VIA_HOST)
			service.update_network(
				{"public_ipv4": pool.host_address or address, "routes": service.get_routes_with(route)}
			)
		else:
			route = Route(IPV6_INTERNET_DESTINATION, ROUTE_VIA_HOST)
			service.update_network({"public_ipv6": prefix, "routes": service.get_routes_with(route)})

	def _move_provider_pool(self, pool: PublicIPPool, server: str) -> None:
		if pool.provider_status != "Attached" or pool.attached_server == server:
			return
		provider = frappe.get_single("Atlas Settings").server_provider_controller
		provider.detach_public_ip_address(
			pool.provider_resource_id,
			pool.host_address,
			frappe.get_doc("Metal Server", pool.attached_server),
		)
		pool.db_set({"provider_status": "Available", "attached_server": None, "host_address": None})
		pool.reload()

	def _apply_detach(self, pool: PublicIPPool, virtual_machine: VirtualMachine) -> None:
		from atlas.vm.core.vm_service import VirtualMachineService

		if virtual_machine.is_terminating:
			return
		service = VirtualMachineService(virtual_machine)
		service.lock_network()
		# A VM that Metal does not hold has no network to change.
		if not service.get_information():
			return
		if pool.is_routed:
			service.update_network({"routes": service.get_routes_without(IPV6_INTERNET_DESTINATION)})
		elif pool.version == "4":
			service.update_network({"public_ipv4": ""})
		else:
			service.update_network(
				{"public_ipv6": "", "routes": service.get_routes_without(IPV6_INTERNET_DESTINATION)}
			)

	def _complete_attach(self, name: str, intent_version: int) -> None:
		allocation = frappe.qb.DocType("Public IP Allocation")
		(
			frappe.qb.update(allocation)
			.set(allocation.status, "Attached")
			.set(allocation.failure_message, None)
			.where(allocation.name == name)
			.where(allocation.intent_version == intent_version)
		).run()

	def _complete_detach(self, name: str, intent_version: int, is_routed: bool, is_reserved: bool) -> None:
		current = frappe.db.get_value(
			"Public IP Allocation", name, ["intent_version", "status", "tenant_id"], as_dict=True
		)
		if not current or current.intent_version != intent_version or current.status != "Detaching":
			return
		if is_routed:
			frappe.db.set_value(
				"Public IP Allocation",
				name,
				{
					"status": "Available",
					"tenant_id": UNOWNED_TENANT_ID,
					"virtual_machine": None,
					"server": None,
				},
				update_modified=False,
			)
			frappe.delete_doc("Public IP Allocation", name, ignore_permissions=True, delete_permanently=True)
			return
		values = {
			"status": "Reserved" if is_reserved else "Available",
			"tenant_id": current.tenant_id if is_reserved else UNOWNED_TENANT_ID,
			"virtual_machine": None,
			"server": None,
			"failure_message": None,
		}
		frappe.db.set_value("Public IP Allocation", name, values, update_modified=False)


def replenish_direct_allocations() -> None:
	for name in frappe.get_all(
		"Public IP Pool",
		filters={"enabled": 1, "gateway": ["is", "not set"], "source": "Static"},
		pluck="name",
	):
		pool: PublicIPPool = frappe.get_doc("Public IP Pool", name, for_update=True)
		if not pool.enabled or pool.is_routed or pool.source == "Provider":
			continue
		available = frappe.db.count("Public IP Allocation", {"pool": pool.name, "status": "Available"})
		if available <= ALLOCATION_REPLENISH_THRESHOLD:
			_generate_available_allocations(pool, ALLOCATION_BATCH_SIZE)


def generate_available_allocations(pool_name: str, limit: int = ALLOCATION_BATCH_SIZE) -> int:
	"""Create the next direct allocation records and return the number created."""
	pool: PublicIPPool = frappe.get_doc("Public IP Pool", pool_name, for_update=True)
	if pool.is_routed:
		raise ValueError("Routed pools create allocations when a VM requests IPv6")
	if pool.source == "Provider":
		raise ValueError("Provider pools create allocations when an address is requested")
	return _generate_available_allocations(pool, limit)


def _generate_available_allocations(pool: PublicIPPool, limit: int) -> int:
	service = PublicIPService()
	created = 0
	for _allocation_index in range(min(max(int(limit), 0), ALLOCATION_BATCH_SIZE)):
		try:
			service._create_next(pool)
		except PublicIPPoolEmpty:
			break
		created += 1
	return created
