# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address

if TYPE_CHECKING:
	from collections.abc import Iterator

	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

DEFAULT_LISTEN_PORT = 51820
ORPHANED_GATEWAY_MINUTES = 30


class WireGuardGatewayServer(Document):
	"""Own one WireGuard gateway and its virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_token: DF.Password | None
		failure_message: DF.SmallText | None
		gateway_public_key: DF.Data | None
		installation_task: DF.Link | None
		listen_port: DF.Int | None
		server: DF.Link | None
		status: DF.Literal["Pending", "Provisioning", "Active", "Failed", "Archived"]
		virtual_machine: DF.Link | None
		wireguard_mesh_ipv6: DF.Data | None
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Reject records created outside the WireGuard Gateway API."""
		if not getattr(self.flags, "created_by_wg_gateway_api", False):
			frappe.throw(_("Create WireGuard Gateway Servers from the WireGuard Gateway Server list."))

	def on_trash(self) -> None:
		"""Refuse deletion while the virtual machine exists."""
		if self.status != "Archived" and self.virtual_machine:
			frappe.throw(_("Archive WireGuard Gateway Server {0} before you delete it.").format(self.name))

	@staticmethod
	def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
		"""Create a WireGuard Gateway Server and its virtual machine, then queue setup."""
		_validate_system_manager()
		values = frappe.parse_json(request) if isinstance(request, str) else request
		if not isinstance(values, dict):
			frappe.throw(_("WireGuard Gateway Server creation data must be an object."))
		_validate_create_request(values)

		gateway = frappe.new_doc("WireGuard Gateway Server")
		gateway.flags.created_by_wg_gateway_api = True
		gateway.listen_port = values.get("listen_port") or DEFAULT_LISTEN_PORT
		gateway.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

		is_draft = gateway._create_virtual_machine(values)
		gateway.save(ignore_permissions=True)
		if gateway.status != "Failed":
			gateway.enqueue_provisioning()
		return {"name": gateway.name, "is_draft": is_draft}

	@frappe.whitelist(methods=["POST"])
	def archive(self) -> None:
		"""Terminate the gateway virtual machine. Termination releases its addresses."""
		_validate_system_manager()
		with wireguard_gateway_lifecycle_lock(self.name):
			gateway: WireGuardGatewayServer = frappe.get_doc(self.doctype, self.name)
			if gateway.status == "Archived":
				return

			if frappe.db.exists("WireGuard Gateway Peer", {"gateway": gateway.name}):
				frappe.throw(_("Delete all WireGuard peers before you archive this gateway."))

			try:
				if gateway.virtual_machine and frappe.db.exists("Virtual Machine", gateway.virtual_machine):
					virtual_machine = frappe.get_doc("Virtual Machine", gateway.virtual_machine)
					virtual_machine.set_termination_protection(False)
					virtual_machine.terminate()
			except Exception as error:
				gateway.status = "Failed"
				gateway.failure_message = f"archive: {error}"
				gateway.save(ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				raise

			gateway.status = "Archived"
			gateway.failure_message = None
			gateway.virtual_machine = None
			gateway.installation_task = None
			gateway.gateway_public_key = None
			gateway.save(ignore_permissions=True)

		frappe.msgprint(_("WireGuard Gateway Server {0} is archived.").format(self.name))

	@frappe.whitelist(methods=["POST"])
	def sync_peers(self) -> None:
		"""Push the desired peer list to the gateway again."""
		_validate_system_manager()
		from atlas.service.core.wg_gateway.peers import push

		push(self.name)
		frappe.msgprint(_("WireGuard Gateway Server {0} peers are synced.").format(self.name))

	def enqueue_provisioning(self, enqueue_after_commit: bool = True) -> None:
		"""Queue gateway setup for the attached virtual machine."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_provision",
			queue="long",
			timeout=3_600,
			job_id=f"atlas||wireguard-gateway-server||provision||{self.name}",
			deduplicate=True,
			enqueue_after_commit=enqueue_after_commit,
		)

	def _provision(self) -> None:
		from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner

		WireGuardGatewayProvisioner(self).run()

	def _create_virtual_machine(self, values: dict[str, Any]) -> bool:
		from atlas.vm.core.placement import OutOfCapacity, PlacementBusy
		from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService

		request = _virtual_machine_request(values, self.name)
		try:
			result = VirtualMachineService.create(request)
			self._set_virtual_machine(result["name"])
			return bool(result["is_draft"])
		except (OutOfCapacity, PlacementBusy) as error:
			self.status = "Failed"
			self.failure_message = f"placement: {error}"
			return False
		except VirtualMachineCreateError as error:
			self._set_virtual_machine(error.virtual_machine_name)
			self.status = "Failed"
			self.failure_message = f"virtual-machine: {error}"
			return True

	def _set_virtual_machine(self, name: str) -> None:
		"""Store the gateway VM and its stable mesh address."""
		self.virtual_machine = name
		self.wireguard_mesh_ipv6 = get_virtual_machine_mesh_address(frappe._dict(name=name, tenant_id=0))


@frappe.whitelist(methods=["POST"])
def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
	"""Call the WireGuard Gateway Server creation service from the list view."""
	_validate_system_manager()
	return WireGuardGatewayServer.create(request)


def _virtual_machine_request(values: dict[str, Any], hostname: str) -> dict[str, Any]:
	"""Return the gateway virtual machine request for validation and creation."""
	return {
		"virtual_machine_image": values.get("virtual_machine_image"),
		"cpu_millicores": values.get("cpu_millicores"),
		"memory_mib": values.get("memory_mib"),
		"disk_mib": values.get("disk_mib"),
		"tenant_id": 0,
		"is_privileged": True,
		"is_termination_protected": True,
		"hostname": hostname,
		"ssh_keys": frappe.get_single("Atlas Settings").public_ssh_key,
		"public_ipv4": values.get("public_ipv4"),
	}


def _validate_create_request(values: dict[str, Any]) -> None:
	"""Reject a create request that cannot produce a working gateway."""
	from atlas.vm.core.models import VirtualMachineCreateRequest
	from atlas.vm.core.vm_service import VirtualMachineService

	try:
		request = VirtualMachineCreateRequest.from_value(_virtual_machine_request(values, "wg-gateway"))
	except ValueError as error:
		frappe.throw(_(str(error)), exc=AtlasUserError)
		raise AssertionError from error

	image = VirtualMachineService.get_image(request.virtual_machine_image, request.tenant_id)
	if image.image_type != "system":
		frappe.throw(_("Select a System Virtual Machine Image."))
	image.validate_compatibility(request.disk_mib)

	_validate_ipv4_allocation(request.public_ipv4)
	_validate_listen_port(values.get("listen_port") or DEFAULT_LISTEN_PORT)


def _validate_ipv4_allocation(allocation_name: object) -> None:
	"""Require a free IPv4 allocation reserved by tenant 0."""
	if not isinstance(allocation_name, str) or not frappe.db.exists("Public IP Allocation", allocation_name):
		frappe.throw(_("Select a reserved public IPv4 allocation."))
	allocation = frappe.get_doc("Public IP Allocation", allocation_name)
	if allocation.version != "4" or allocation.status != "Reserved" or allocation.tenant_id != 0:
		frappe.throw(_("Select a free public IPv4 allocation reserved by tenant 0."))


def _validate_listen_port(port: object) -> None:
	"""Require a valid UDP listen port for the WireGuard endpoint."""
	if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
		frappe.throw(_("Listen port must be an integer from 1 to 65535."))


@contextmanager
def wireguard_gateway_lifecycle_lock(name: str) -> Iterator[None]:
	"""Serialize lifecycle actions of one gateway."""
	try:
		with filelock(f"atlas:wireguard-gateway-server:{name}", timeout=0):
			yield
	except LockTimeoutError:
		frappe.throw(
			_("Another lifecycle action is in progress for WireGuard Gateway Server {0}.").format(name)
		)


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	user_type = frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type != "System User":
		frappe.throw(_("Only System Users can manage WireGuard Gateway Servers."), frappe.PermissionError)


def enqueue_pending_gateway_provisioning() -> None:
	"""Continue gateway setup after virtual machine reconciliation or an interrupted job."""
	for name in frappe.get_all(
		"WireGuard Gateway Server",
		filters={"status": ["in", ["Pending", "Provisioning"]], "virtual_machine": ["is", "set"]},
		pluck="name",
	):
		frappe.get_doc("WireGuard Gateway Server", name).enqueue_provisioning(enqueue_after_commit=False)
	fail_orphaned_gateways()


def fail_orphaned_gateways() -> None:
	"""Fail a pending gateway whose virtual machine was never linked."""
	cutoff = add_to_date(now_datetime(), minutes=-ORPHANED_GATEWAY_MINUTES)
	for name in frappe.get_all(
		"WireGuard Gateway Server",
		filters={"status": "Pending", "virtual_machine": ["is", "not set"], "creation": ["<", cutoff]},
		pluck="name",
	):
		frappe.db.set_value(
			"WireGuard Gateway Server",
			name,
			{
				"status": "Failed",
				"failure_message": (
					"virtual-machine: no virtual machine was linked. "
					"Check for an unlinked Virtual Machine holding the public IPv4 allocation."
				),
			},
		)
