# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.service.core.ipv6_router.address import (
	parse_ipv6_network,
	validate_router_network,
)

if TYPE_CHECKING:
	from collections.abc import Iterator

	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


class IPv6RouterServer(Document):
	"""Own one IPv6 router and its virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		failure_message: DF.SmallText | None
		installation_task: DF.Link | None
		server: DF.Link | None
		status: DF.Literal["Pending", "Provisioning", "Active", "Failed", "Archived"]
		virtual_machine: DF.Link | None
		wireguard_mesh_ipv6: DF.Data | None
	# end: auto-generated types

	@property
	def prefix(self) -> str:
		"""Return the public IPv6 block that the router translates."""
		return frappe.db.get_value("Public IP Pool", {"gateway": self.name}, "prefix") or ""

	@property
	def pool(self):
		"""Return the public IP pool that this router serves."""
		name = frappe.db.get_value("Public IP Pool", {"gateway": self.name})
		return frappe.get_doc("Public IP Pool", name) if name else None

	def before_insert(self) -> None:
		"""Reject records created outside the IPv6 Router Server API."""
		if not getattr(self.flags, "created_by_ipv6_router_api", False):
			frappe.throw(_("Create IPv6 Router Servers from the IPv6 Router Server list."))

	def on_trash(self) -> None:
		"""Refuse deletion while the virtual machine exists."""
		if self.status != "Archived" and self.virtual_machine:
			frappe.throw(_("Archive IPv6 Router Server {0} before you delete it.").format(self.name))

	@staticmethod
	def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
		"""Create an IPv6 Router Server and its virtual machine, then queue setup."""
		_validate_system_manager()
		values = frappe.parse_json(request) if isinstance(request, str) else request
		if not isinstance(values, dict):
			frappe.throw(_("IPv6 Router Server creation data must be an object."))
		_validate_create_request(values)

		pool = frappe.get_doc("Public IP Pool", values["public_ip_pool"], for_update=True)
		router = frappe.new_doc("IPv6 Router Server")
		router.flags.created_by_ipv6_router_api = True
		router.insert(ignore_permissions=True)
		pool.allocation_prefix_length = 128
		pool.gateway = router.name
		pool.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

		is_draft = router._create_virtual_machine(values)
		router.save(ignore_permissions=True)
		if router.status != "Failed":
			router.enqueue_provisioning()
		return {"name": router.name, "is_draft": is_draft}

	@frappe.whitelist(methods=["POST"])
	def archive(self) -> None:
		"""Terminate the router virtual machine. Termination releases its addresses."""
		_validate_system_manager()
		with ipv6_router_lifecycle_lock(self.name):
			router: IPv6RouterServer = frappe.get_doc(self.doctype, self.name)
			if router.status == "Archived":
				return

			pool = router.pool
			if pool and frappe.db.exists(
				"Public IP Allocation",
				{"pool": pool.name, "status": ["in", ["Attaching", "Attached", "Detaching"]]},
			):
				frappe.throw(_("Detach all routed IPv6 allocations before you archive this router."))

			try:
				if pool:
					pool.begin_provider_detach()
					# Archive runs in a request. A worker detaches the block from the host.
					pool.queue_reconcile()
				if router.virtual_machine and frappe.db.exists("Virtual Machine", router.virtual_machine):
					virtual_machine = frappe.get_doc("Virtual Machine", router.virtual_machine)
					virtual_machine.set_termination_protection(False)
					virtual_machine.terminate()
			except Exception as error:
				router.status = "Failed"
				router.failure_message = f"archive: {error}"
				router.save(ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				raise

			router.status = "Archived"
			router.failure_message = None
			router.virtual_machine = None
			router.installation_task = None
			router.save(ignore_permissions=True)
			if pool:
				pool.gateway = None
				# A direct provider pool hands out its whole prefix.
				if pool.source == "Provider":
					pool.allocation_prefix_length = ipaddress.ip_network(pool.prefix, strict=False).prefixlen
				pool.save(ignore_permissions=True)

		frappe.msgprint(_("IPv6 Router Server {0} is archived.").format(self.name))

	def enqueue_provisioning(self, enqueue_after_commit: bool = True) -> None:
		"""Queue router setup for the attached virtual machine."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_provision",
			queue="long",
			timeout=3_600,
			job_id=f"atlas||ipv6-router-server||provision||{self.name}",
			deduplicate=True,
			enqueue_after_commit=enqueue_after_commit,
		)

	def _provision(self) -> None:
		from atlas.service.core.ipv6_router.provisioning import IPv6RouterServerProvisioner

		IPv6RouterServerProvisioner(self).run()

	def _create_virtual_machine(self, values: dict[str, Any]) -> bool:
		from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService

		request = {
			"virtual_machine_image": values.get("virtual_machine_image"),
			"cpu_millicores": values.get("cpu_millicores"),
			"memory_mib": values.get("memory_mib"),
			"disk_mib": values.get("disk_mib"),
			"tenant_id": 0,
			"is_privileged": True,
			"is_termination_protected": True,
			"hostname": self.name,
			"ssh_keys": frappe.get_single("Atlas Settings").public_ssh_key,
			"public_ipv4": values["public_ipv4"],
		}
		try:
			result = VirtualMachineService.create(request)
			self._set_virtual_machine(result["name"])
			return bool(result["is_draft"])
		except VirtualMachineCreateError as error:
			self._set_virtual_machine(error.virtual_machine_name)
			self.status = "Failed"
			self.failure_message = f"virtual-machine: {error}"
			return True

	def _set_virtual_machine(self, name: str) -> None:
		"""Store the router VM and its stable mesh address."""
		self.virtual_machine = name
		self.wireguard_mesh_ipv6 = get_virtual_machine_mesh_address(frappe._dict(name=name, tenant_id=0))


@frappe.whitelist(methods=["POST"])
def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
	"""Call the IPv6 Router Server creation service from the list view."""
	return IPv6RouterServer.create(request)


def _validate_create_request(values: dict[str, Any]) -> None:
	"""Reject a create request that cannot produce a working router."""
	from atlas.vm.core.models import VirtualMachineCreateRequest
	from atlas.vm.core.vm_service import VirtualMachineService

	virtual_machine_request = {
		"virtual_machine_image": values.get("virtual_machine_image"),
		"cpu_millicores": values.get("cpu_millicores"),
		"memory_mib": values.get("memory_mib"),
		"disk_mib": values.get("disk_mib"),
		"tenant_id": 0,
		"is_privileged": True,
		"is_termination_protected": True,
		"hostname": "ipv6-router",
		"ssh_keys": frappe.get_single("Atlas Settings").public_ssh_key,
		"public_ipv4": values.get("public_ipv4"),
	}
	try:
		request = VirtualMachineCreateRequest.from_value(virtual_machine_request)
	except ValueError as error:
		frappe.throw(_(str(error)), exc=AtlasUserError)
		raise AssertionError from error

	image = VirtualMachineService.get_image(request.virtual_machine_image, request.tenant_id)
	if image.image_type != "system":
		frappe.throw(_("Select a System Virtual Machine Image."))
	image.validate_compatibility(request.disk_mib)

	_validate_ipv4_allocation(request.public_ipv4)
	_validate_ipv6_pool(values.get("public_ip_pool"))


def _validate_ipv4_allocation(allocation_name: object) -> None:
	"""Require a free IPv4 allocation reserved by tenant 0."""
	if not isinstance(allocation_name, str) or not frappe.db.exists("Public IP Allocation", allocation_name):
		frappe.throw(_("Select a reserved public IPv4 allocation."))
	allocation = frappe.get_doc("Public IP Allocation", allocation_name)
	if allocation.version != "4" or allocation.status != "Reserved" or allocation.tenant_id != 0:
		frappe.throw(_("Select a free public IPv4 allocation reserved by tenant 0."))


def _validate_ipv6_pool(pool_name: object) -> None:
	"""Require a free IPv6 pool that can use the router address layout."""
	if not isinstance(pool_name, str) or not frappe.db.exists("Public IP Pool", pool_name):
		frappe.throw(_("Select a public IPv6 pool."))
	pool = frappe.get_doc("Public IP Pool", pool_name)
	if pool.version != "6" or pool.gateway or frappe.db.exists("Public IP Allocation", {"pool": pool.name}):
		frappe.throw(_("Select an IPv6 pool that has no gateway or allocations."))
	validate_router_network(parse_ipv6_network(pool.prefix))


@contextmanager
def ipv6_router_lifecycle_lock(name: str) -> Iterator[None]:
	"""Serialize lifecycle actions of one router."""
	try:
		with filelock(f"atlas:ipv6-router-server:{name}", timeout=0):
			yield
	except LockTimeoutError:
		frappe.throw(_("Another lifecycle action is in progress for IPv6 Router Server {0}.").format(name))


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	user_type = frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type != "System User":
		frappe.throw(_("Only System Users can manage IPv6 Router Servers."), frappe.PermissionError)


def enqueue_pending_ipv6_router_provisioning() -> None:
	"""Continue router setup after virtual machine reconciliation or an interrupted job."""
	for name in frappe.get_all(
		"IPv6 Router Server",
		filters={"status": ["in", ["Pending", "Provisioning"]], "virtual_machine": ["is", "set"]},
		pluck="name",
	):
		frappe.get_doc("IPv6 Router Server", name).enqueue_provisioning(enqueue_after_commit=False)
