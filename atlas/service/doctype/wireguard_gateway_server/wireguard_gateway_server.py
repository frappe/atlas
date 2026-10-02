# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.mesh_address import get_region_mesh_address_prefix, get_virtual_machine_mesh_address
from atlas.metal_server.core.atlas_peer import generate_private_key, get_public_key

if TYPE_CHECKING:
	from collections.abc import Iterator

DEFAULT_LISTEN_PORT = 51820
ORPHANED_GATEWAY_MINUTES = 30
ANYWHERE = ["0.0.0.0/0", "::/0"]
REGIONAL_SUBDOMAIN = "wireguard"
# The record keeps the shape of its virtual machine, so Rebuild never needs the old machine.
SHAPE_FIELDS = ("virtual_machine_image", "cpu_millicores", "memory_mib", "disk_mib")


def get_gateway_firewall(region_id: int, listen_port: int) -> dict[str, Any]:
	"""Admit tunnels, the API, and ICMP from anywhere, and the region mesh, which carries VM replies and peers."""
	return {
		"enabled": True,
		"inbound": [
			{"protocol": "any", "cidrs": [f"{get_region_mesh_address_prefix(region_id)}::/32"]},
			{"protocol": "icmp", "cidrs": ANYWHERE},
			{"protocol": "udp", "ports": str(listen_port), "cidrs": ANYWHERE},
			{"protocol": "tcp", "ports": "443", "cidrs": ANYWHERE},
		],
		"outbound": [{"protocol": "any", "cidrs": ANYWHERE}],
	}


class WireguardGatewayServer(Document):
	"""One node of the regional WireGuard gateway cluster and its virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cpu_millicores: DF.Int
		disk_mib: DF.Int
		dns_health_check_id: DF.Data | None
		failure_message: DF.SmallText | None
		gateway_public_key: DF.Data | None
		installed_package_hash: DF.Data | None
		is_cluster_member: DF.Check
		listen_port: DF.Int
		memory_mib: DF.Int
		private_key: DF.Password | None
		pushed_config_hash: DF.Data | None
		server: DF.Link | None
		status: DF.Literal["Pending", "Provisioning", "Active", "Failed", "Archived"]
		virtual_machine: DF.Link | None
		virtual_machine_image: DF.Data | None
		wireguard_mesh_ipv6: DF.Data | None
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Reject records created outside the creation service, and give the node its WireGuard key."""
		if not getattr(self.flags, "created_by_wg_gateway_api", False):
			frappe.throw(_("Create Wireguard Gateway Servers from the Wireguard Gateway Server list."))

		self.private_key = generate_private_key()
		self.gateway_public_key = get_public_key(self.private_key)

	def on_trash(self) -> None:
		"""Refuse deletion while the virtual machine exists."""
		if self.status != "Archived" and self.virtual_machine:
			frappe.throw(_("Archive Wireguard Gateway Server {0} before you delete it.").format(self.name))

	@property
	def gateway_id(self) -> int:
		"""Return the node number, which the client addresses of this node carry."""
		return int(self.name.rsplit("-", 1)[-1])

	@property
	def public_ipv4(self) -> str | None:
		if not self.virtual_machine:
			return None
		return frappe.get_doc("Virtual Machine", self.virtual_machine).public_ipv4

	def get_domain(self) -> str:
		"""Return the node name, which devices use as their WireGuard endpoint."""
		return f"{self.name}.{frappe.get_cached_value('Atlas Settings', 'Atlas Settings', 'wildcard_domain')}"

	@staticmethod
	def get_regional_domain() -> str:
		"""Return the API name that lists every active node."""
		return f"{REGIONAL_SUBDOMAIN}.{frappe.get_cached_value('Atlas Settings', 'Atlas Settings', 'wildcard_domain')}"

	@staticmethod
	def create(request: str | dict[str, Any]) -> dict[str, Any]:
		"""Create a gateway node and its virtual machine, then queue setup."""
		values = frappe.parse_json(request) if isinstance(request, str) else request
		if not isinstance(values, dict):
			frappe.throw(_("Wireguard Gateway Server creation data must be an object."))
		_validate_create_request(values)

		gateway = frappe.new_doc("Wireguard Gateway Server")
		gateway.flags.created_by_wg_gateway_api = True
		gateway.listen_port = values.get("listen_port") or DEFAULT_LISTEN_PORT
		gateway.update({field: values.get(field) for field in SHAPE_FIELDS})
		gateway.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

		is_draft = gateway._create_virtual_machine(values)
		gateway.save(ignore_permissions=True)
		if gateway.status != "Failed":
			gateway.enqueue_provisioning()
		return {
			"name": gateway.name,
			"is_draft": is_draft,
			"api_url": f"https://{gateway.get_regional_domain()}",
		}

	@frappe.whitelist(methods=["POST"])
	def archive(self) -> None:
		"""Remove the node from the cluster and DNS, then terminate its virtual machine. Safe to run again."""
		_validate_system_manager()
		with wireguard_gateway_lifecycle_lock(self.name):
			gateway: WireguardGatewayServer = frappe.get_doc(self.doctype, self.name)
			if gateway.status == "Archived":
				return

			gateway.leave_cluster("archive")
			gateway.terminate_virtual_machine()
			gateway.update(
				{
					"status": "Archived",
					"failure_message": None,
					"virtual_machine": None,
					"installed_package_hash": None,
					"pushed_config_hash": None,
				}
			)
			gateway.save(ignore_permissions=True)

		frappe.msgprint(_("WireGuard Gateway Server {0} is archived.").format(self.name))

	@frappe.whitelist(methods=["POST"])
	def rebuild(self, public_ipv4: str) -> None:
		"""Replace the virtual machine of this node with the recorded shape. Safe to run again.

		The node keeps its name, key, and devices, and restores the device table from the other nodes.
		A single node needs a restore through the API."""
		_validate_system_manager()
		with wireguard_gateway_lifecycle_lock(self.name):
			gateway: WireguardGatewayServer = frappe.get_doc(self.doctype, self.name)
			if gateway.status == "Archived":
				frappe.throw(_("WireGuard Gateway Server {0} is archived.").format(self.name))

			values = {field: gateway.get(field) for field in SHAPE_FIELDS}
			values |= {"public_ipv4": public_ipv4, "listen_port": gateway.listen_port}
			_validate_create_request(values)

			gateway.leave_cluster("rebuild")
			gateway.terminate_virtual_machine()
			gateway.update(
				{
					"status": "Pending",
					"failure_message": None,
					"installed_package_hash": None,
					"pushed_config_hash": None,
				}
			)
			gateway._create_virtual_machine(values)
			gateway.save(ignore_permissions=True)
			if gateway.status != "Failed":
				gateway.enqueue_provisioning()

		frappe.msgprint(_("WireGuard Gateway Server {0} is rebuilding.").format(self.name))

	def terminate_virtual_machine(self) -> None:
		"""Terminate the node's virtual machine. A machine that is already terminating or gone is skipped."""
		if not self.virtual_machine or not frappe.db.exists("Virtual Machine", self.virtual_machine):
			return
		virtual_machine = frappe.get_doc("Virtual Machine", self.virtual_machine)
		if virtual_machine.is_terminating:
			return
		virtual_machine.set_termination_protection(False)
		virtual_machine.terminate()

	def leave_cluster(self, action: str) -> None:
		"""Drain the node, then make the other nodes drop it. Each step commits before the next external change.

		The record stays Failed until the action ends, so a failed run shows where it stopped and can run again."""
		from atlas.service.core.wg_gateway.provisioning import (
			WireGuardGatewayProvisioner,
			apply_membership_to_active_gateways,
		)

		self.update({"status": "Failed", "failure_message": f"{action}: in progress", "is_cluster_member": 0})
		self.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

		self.remove_dns_records()
		self.dns_health_check_id = None
		self.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		WireGuardGatewayProvisioner(self).stop_api()

		apply_membership_to_active_gateways(excluding=self.name)

	def remove_dns_records(self) -> None:
		"""Remove the regional and node records. A record that is already gone is not an error."""
		provider = frappe.get_single("Atlas Settings").dns_provider_controller
		provider.remove_multivalue_a_record(self.get_regional_domain(), self.name)
		if self.dns_health_check_id:
			provider.remove_health_check(self.dns_health_check_id)
		provider.remove_a_record(self.get_domain())

	def enqueue_provisioning(self, enqueue_after_commit: bool = True) -> None:
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

	def enqueue_configuration_push(self, enqueue_after_commit: bool = True) -> None:
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_push_configuration",
			queue="long",
			timeout=600,
			job_id=f"atlas||wireguard-gateway-server||configure||{self.name}",
			deduplicate=True,
			enqueue_after_commit=enqueue_after_commit,
		)

	def _provision(self) -> None:
		from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner

		WireGuardGatewayProvisioner(self).run()

	def _push_configuration(self) -> None:
		from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner

		WireGuardGatewayProvisioner(self).push_configuration()

	def _create_virtual_machine(self, values: dict[str, Any]) -> bool:
		from atlas.vm.core.placement import OutOfCapacity, PlacementBusy
		from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService

		try:
			result = VirtualMachineService.create(_virtual_machine_request(values, self.name))
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
def create(request: str | dict[str, Any]) -> dict[str, Any]:
	"""Call the Wireguard Gateway Server creation service from the list view."""
	_validate_system_manager()
	return WireguardGatewayServer.create(request)


def _virtual_machine_request(values: dict[str, Any], hostname: str) -> dict[str, Any]:
	"""Return the gateway virtual machine request for validation and creation."""
	settings = frappe.get_single("Atlas Settings")
	return {
		"virtual_machine_image": values.get("virtual_machine_image"),
		"cpu_millicores": values.get("cpu_millicores"),
		"memory_mib": values.get("memory_mib"),
		"disk_mib": values.get("disk_mib"),
		"tenant_id": 0,
		"is_privileged": True,
		"is_termination_protected": True,
		"hostname": hostname,
		"ssh_keys": settings.public_ssh_key,
		"public_ipv4": values.get("public_ipv4"),
		"firewall": get_gateway_firewall(
			settings.region_id, values.get("listen_port") or DEFAULT_LISTEN_PORT
		),
	}


def _validate_create_request(values: dict[str, Any]) -> None:
	"""Reject a create request that cannot produce a working gateway node."""
	from atlas.vm.core.models import VirtualMachineCreateRequest
	from atlas.vm.core.vm_service import VirtualMachineService

	try:
		request = VirtualMachineCreateRequest.from_value(_virtual_machine_request(values, "wireguard"))
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
	"""Serialize lifecycle actions of one gateway node."""
	try:
		with filelock(f"atlas:wireguard-gateway-server:{name}", timeout=0):
			yield
	except LockTimeoutError:
		frappe.throw(
			_("Another lifecycle action is in progress for Wireguard Gateway Server {0}.").format(name)
		)


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	user_type = frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type != "System User":
		frappe.throw(_("Only System Users can manage Wireguard Gateway Servers."), frappe.PermissionError)


def gateway_client_prefix(region: int, name: str) -> str:
	"""Return the /48 of client addresses that one gateway node owns."""
	identifier = int(name.rsplit("-", 1)[-1])
	address = (0xFDAC << 112) | (region << 96) | (identifier << 80)
	return str(ipaddress.IPv6Network((address, 48)))


def get_wireguard_gateway_routes() -> list[dict[str, str]]:
	"""Return the return route of every Active node, which each host adds to opted-in VMs."""
	region = frappe.get_single("Atlas Settings").region_id
	return [
		{"destination": gateway_client_prefix(region, gateway.name), "via": gateway.wireguard_mesh_ipv6}
		for gateway in frappe.get_all(
			"Wireguard Gateway Server",
			filters={"status": "Active", "wireguard_mesh_ipv6": ["is", "set"]},
			fields=["name", "wireguard_mesh_ipv6"],
			order_by="name asc",
		)
	]


def enqueue_pending_gateway_provisioning() -> None:
	"""Continue gateway setup after virtual machine reconciliation or an interrupted job."""
	for name in frappe.get_all(
		"Wireguard Gateway Server",
		filters={"status": ["in", ["Pending", "Provisioning"]], "virtual_machine": ["is", "set"]},
		pluck="name",
	):
		frappe.get_doc("Wireguard Gateway Server", name).enqueue_provisioning(enqueue_after_commit=False)
	fail_orphaned_gateways()


def fail_orphaned_gateways() -> None:
	"""Fail a pending gateway whose virtual machine was never linked."""
	cutoff = add_to_date(now_datetime(), minutes=-ORPHANED_GATEWAY_MINUTES)
	for name in frappe.get_all(
		"Wireguard Gateway Server",
		filters={"status": "Pending", "virtual_machine": ["is", "not set"], "creation": ["<", cutoff]},
		pluck="name",
	):
		frappe.db.set_value(
			"Wireguard Gateway Server",
			name,
			{
				"status": "Failed",
				"failure_message": (
					"virtual-machine: no virtual machine was linked. "
					"Check for an unlinked Virtual Machine holding the public IPv4 allocation."
				),
			},
		)
