from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Never, TypedDict, cast

import frappe
from frappe import _

from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.metal_server.core.public_ip_service import PublicIPService
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalVirtualMachine, MetalVirtualMachineMetrics
from atlas.vm.core.models import (
	IPV4_INTERNET_DESTINATION,
	IPV6_INTERNET_DESTINATION,
	ROUTE_VIA_HOST,
	FirewallConfiguration,
	Route,
	VirtualMachineCreateRequest,
	parse_routes,
)
from atlas.vm.core.placement import PlacementRequirements, PlacementStrategy
from atlas.vm.core.placement.transaction import use_read_committed

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


class MetalOperationError(frappe.ValidationError):
	"""Report that the assigned host could not complete the request."""

	http_status_code = 502


class InsufficientHostCapacity(AtlasUserError):
	"""The assigned host has no room for an online increase."""

	code = "insufficient_capacity"
	http_status_code = 409


class VirtualMachineCreateError(MetalOperationError):
	"""Report a failed create and the draft that records it."""

	def __init__(self, virtual_machine_name: str, error: MetalClientError) -> None:
		super().__init__(_("Metal request failed: {0}").format(error))
		self.virtual_machine_name = virtual_machine_name


class VirtualMachineCreateResult(TypedDict):
	"""The stored result of one virtual machine create request."""

	name: str
	is_draft: bool


class VirtualMachineService:
	"""Own Atlas orchestration for one virtual machine."""

	def __init__(self, virtual_machine: VirtualMachine) -> None:
		self.virtual_machine = virtual_machine

	@classmethod
	@use_read_committed
	def create(cls, value: str | dict[str, Any] | VirtualMachineCreateRequest) -> VirtualMachineCreateResult:
		"""Create and commit an Atlas request before the Metal request."""
		if isinstance(value, VirtualMachineCreateRequest):
			request = value
		else:
			try:
				request = VirtualMachineCreateRequest.from_value(value)
			except ValueError as error:
				frappe.throw(_(str(error)))
				raise AssertionError from error

		image = cls.get_image(request.virtual_machine_image, request.tenant_id)
		image.validate_compatibility(request.disk_mib)
		requirements = PlacementRequirements(
			request.cpu_millicores,
			request.memory_mib,
			request.disk_mib,
			image.architecture,
			request.tenant_id,
			request.sleep_after_idle_seconds > 0,
			placement_rules=request.placement_rules,
		)
		server_name = PlacementStrategy.find_server(requirements)
		virtual_machine = cls.insert_draft(request, image, server_name)
		service = cls(virtual_machine)
		for version, selector in ((4, request.public_ipv4), (6, request.public_ipv6)):
			if selector:
				PublicIPService().attach(virtual_machine, version, selector)
		metal_request = service.get_metal_request(request, image)
		frappe.db.commit()  # nosemgrep

		virtual_machine_name = cast(str, virtual_machine.name)
		try:
			service.metal_client.put_virtual_machine(virtual_machine_name, metal_request)
		except MetalClientError as error:
			if error.uncertain:
				return {"name": virtual_machine_name, "is_draft": True}
			raise VirtualMachineCreateError(virtual_machine_name, error) from error

		virtual_machine.is_draft = 0
		virtual_machine.save()
		return {"name": virtual_machine_name, "is_draft": False}

	@staticmethod
	def get_image(image_name: str, tenant_id: int) -> VirtualMachineImage:
		"""Return one enabled and available virtual machine image."""
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		if not image.is_visible_to_tenant(tenant_id):
			frappe.throw(
				_("Virtual Machine Image {0} is not available.").format(image.title),
				exc=frappe.DoesNotExistError,
			)
		if not image.enabled:
			frappe.throw(_("Virtual Machine Image {0} is disabled.").format(image.title), exc=AtlasUserError)
		image.validate_is_available()
		return image

	@staticmethod
	def insert_draft(
		request: VirtualMachineCreateRequest, image: VirtualMachineImage, server_name: str
	) -> VirtualMachine:
		"""Insert one draft that reserves capacity before a Metal request.

		The draft copies the image architecture, so later placement never reads the
		image again and the image stays deletable.
		"""
		virtual_machine = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"is_draft": 1,
				"server": server_name,
				"virtual_machine_image": image.name,
				"architecture": image.architecture,
				"cpu_millicores": request.cpu_millicores,
				"memory_mib": request.memory_mib,
				"disk_mib": request.disk_mib,
				"tenant_id": request.tenant_id,
				"is_privileged": request.is_privileged,
				"is_termination_protected": request.is_termination_protected,
				"sleep_after_idle_seconds": request.sleep_after_idle_seconds,
				"tags": [{"key": key, "value": value} for key, value in request.tags.items()],
				"placement_rules": frappe.as_json(request.placement_rules.as_list())
				if request.placement_rules.nodes
				else None,
			}
		)
		virtual_machine.flags.created_by_virtual_machine_api = True
		return cast("VirtualMachine", virtual_machine.insert())

	def get_metal_request(
		self,
		request: VirtualMachineCreateRequest,
		image: VirtualMachineImage,
	) -> dict[str, Any]:
		"""Return the complete Metal create request."""
		return {
			"compute": {
				"cpu_millicores": request.cpu_millicores,
				"memory_mib": request.memory_mib,
				"sleep_after_idle_seconds": request.sleep_after_idle_seconds,
			},
			"disk": {
				"size_mib": request.disk_mib,
				"throughput_mibps": request.disk_throughput_mibps,
				"iops": request.disk_iops,
			},
			"image": image.get_metal_image_request(),
			"network": {
				"public_ipv4": "",
				"wireguard_mesh_ipv6": get_virtual_machine_mesh_address(self.virtual_machine),
				"is_network_gateway": bool(self.virtual_machine.is_network_gateway),
				"private_network_throughput_mibps": request.private_network_throughput_mibps,
				"public_network_throughput_mibps": request.public_network_throughput_mibps,
				"routes": [route.as_dict() for route in request.routes],
				"firewall": request.firewall.as_dict(),
			},
			"guest": {
				"hostname": request.hostname,
				"ssh_keys": list(request.ssh_keys),
				"metadata": dict(request.metadata),
				"user_data": request.user_data,
			},
		}

	def get_information(self) -> MetalVirtualMachine | None:
		"""Return Metal state, or no value when the virtual machine is absent."""
		try:
			return self.metal_client.get_virtual_machine(cast(str, self.virtual_machine.name))
		except MetalClientError as error:
			if error.is_not_found:
				return None
			self.raise_metal_error(error)

	def validate_deletion(self) -> None:
		"""Allow deletion only after Metal confirms absence."""
		is_absence_confirmed = getattr(self.virtual_machine.flags, "metal_absence_confirmed", False)
		if self.virtual_machine.is_draft and not is_absence_confirmed:
			frappe.throw(
				_("Wait for Virtual Machine creation reconciliation before deletion."),
				exc=AtlasUserError,
			)

		if not is_absence_confirmed:
			try:
				self.metal_client.get_virtual_machine(cast(str, self.virtual_machine.name))
			except MetalClientError as error:
				if not error.is_not_found:
					self.raise_metal_error(error)
			else:
				frappe.throw(
					_("Terminate Virtual Machine {0} before deletion.").format(self.virtual_machine.name),
					exc=AtlasUserError,
				)
		self.release_public_addresses()
		for name in frappe.get_all(
			"Public IP Allocation", filters={"virtual_machine": self.virtual_machine.name}, pluck="name"
		):
			frappe.get_doc("Public IP Allocation", name).reconcile()

	def terminate(self) -> None:
		"""Request deletion in Metal, then release the public address intent."""
		try:
			self.metal_client.delete_virtual_machine(cast(str, self.virtual_machine.name))
		except MetalClientError as error:
			if not error.is_not_found:
				self.raise_metal_error(error)

		self.virtual_machine.db_set("is_terminating", 1)
		self.release_public_addresses()

	def replace_ssh_keys(self, ssh_keys: list[str]) -> dict[str, Any]:
		"""Replace all desired SSH keys in Metal."""
		information = self.perform_metal_operation(
			lambda metal_client: metal_client.replace_virtual_machine_ssh_keys(
				cast(str, self.virtual_machine.name), ssh_keys
			)
		)
		return information.as_dict()

	def replace_metadata(self, metadata: dict[str, str]) -> dict[str, Any]:
		"""Replace all desired guest metadata in Metal."""
		information = self.perform_metal_operation(
			lambda metal_client: metal_client.replace_virtual_machine_metadata(
				cast(str, self.virtual_machine.name), metadata
			)
		)
		return information.as_dict()

	def set_power_state(self, state: str) -> None:
		"""Set the desired power state in Metal."""
		self.perform_metal_operation(
			lambda metal_client: metal_client.set_virtual_machine_power_state(
				cast(str, self.virtual_machine.name), state
			)
		)

	def get_metrics(
		self, *, start: datetime | None = None, end: datetime | None = None, max_samples: int | None = None
	) -> MetalVirtualMachineMetrics | None:
		try:
			return self.metal_client.get_virtual_machine_metrics(
				cast(str, self.virtual_machine.name), start=start, end=end, max_samples=max_samples
			)
		except MetalClientError as error:
			if error.is_not_found:
				return None
			self.raise_metal_error(error)

	def request_restart(self) -> None:
		"""Request one restart in Metal."""
		self.perform_metal_operation(
			lambda metal_client: metal_client.request_virtual_machine_restart(
				cast(str, self.virtual_machine.name)
			)
		)

	def set_compute(self, compute: dict[str, Any]) -> dict[str, Any]:
		"""Set the complete compute values in Metal."""
		information = self.perform_metal_operation(
			lambda metal_client: metal_client.set_virtual_machine_compute(
				cast(str, self.virtual_machine.name), compute
			)
		)
		return information.as_dict()

	def set_disk(self, size_mib: int, throughput_mibps: int, iops: int) -> dict[str, Any]:
		"""Set the complete disk values in Metal."""
		information = self.perform_metal_operation(
			lambda metal_client: metal_client.set_virtual_machine_disk(
				cast(str, self.virtual_machine.name),
				{"size_mib": size_mib, "throughput_mibps": throughput_mibps, "iops": iops},
			)
		)
		return information.as_dict()

	def update_disk(self, changes: dict[str, int]) -> dict[str, Any]:
		"""Apply selected disk changes and keep the stored disk size in step."""
		current_disk = self.require_information().desired.disk
		size_mib = changes.get("size_mib", current_disk.size_mib)
		if size_mib < current_disk.size_mib:
			frappe.throw(_("Disk size can only increase."), exc=AtlasUserError)

		information = self.set_disk(
			size_mib,
			changes.get("throughput_mibps", current_disk.throughput_mibps),
			changes.get("iops", current_disk.iops),
		)
		if size_mib != current_disk.size_mib:
			self.virtual_machine.db_set("disk_mib", size_mib)

		return information

	def update_network(self, changes: dict[str, Any]) -> dict[str, Any]:
		"""Replace the complete network after applying selected changes."""
		current_network = self.require_information().desired.network
		firewall = {
			"enabled": current_network.firewall.enabled,
			"inbound": [rule.as_dict() for rule in current_network.firewall.inbound],
			"outbound": [rule.as_dict() for rule in current_network.firewall.outbound],
		}
		if "firewall" in changes:
			firewall_change = changes["firewall"]
			if not isinstance(firewall_change, dict):
				raise ValueError("Firewall change must be an object.")
			firewall = {**firewall, **firewall_change}
			firewall = FirewallConfiguration.from_value(firewall).as_dict()
		request = {
			"public_ipv4": current_network.public_ipv4,
			"wireguard_mesh_ipv6": current_network.wireguard_mesh_ipv6,
			"routes": [route.as_dict() for route in current_network.routes],
			"is_network_gateway": current_network.is_network_gateway,
			"is_accessible_via_wireguard_gateway": current_network.is_accessible_via_wireguard_gateway,
			"public_ipv6": current_network.public_ipv6,
			"private_network_throughput_mibps": current_network.private_network_throughput_mibps,
			"public_network_throughput_mibps": current_network.public_network_throughput_mibps,
			**changes,
			"firewall": firewall,
		}
		information = self.perform_metal_operation(
			lambda metal_client: metal_client.set_virtual_machine_network(
				cast(str, self.virtual_machine.name), request
			)
		)
		return information.as_dict()

	def apply_network_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
		"""Validate and apply selected network changes."""
		self.virtual_machine.validate_network_change()
		self.lock_network()
		if "ipv4_internet_access" in changes:
			changes = {**changes}
			changes["routes"] = self._routes_for_ipv4_internet_access(
				bool(changes.pop("ipv4_internet_access"))
			)
		if "wireguard_gateway_access" in changes:
			changes = {**changes}
			is_enabled = bool(changes.pop("wireguard_gateway_access"))
			if is_enabled and self.virtual_machine.is_network_gateway:
				frappe.throw(_("A network gateway cannot use WireGuard gateway access."), exc=AtlasUserError)
			changes["is_accessible_via_wireguard_gateway"] = is_enabled
		if "routes" in changes:
			changes = {
				**changes,
				"routes": [route.as_dict() for route in self.validate_routes(changes["routes"])],
			}

		try:
			return self.update_network(changes)
		except ValueError as error:
			frappe.throw(_(str(error)), exc=AtlasUserError)
			raise AssertionError from error

	def _routes_for_ipv4_internet_access(self, enabled: bool) -> list[dict[str, str]]:
		if enabled:
			return self.get_routes_with(Route(IPV4_INTERNET_DESTINATION, ROUTE_VIA_HOST))
		if self.get_ipv4_address_name():
			frappe.throw(
				_("Detach the public IPv4 address before you turn off IPv4 internet access."),
				exc=AtlasConflictError,
			)
		return self.get_routes_without(IPV4_INTERNET_DESTINATION)

	def lock_network(self) -> None:
		"""Serialize network changes. Each one reads and replaces the complete Metal network."""
		frappe.db.get_value("Virtual Machine", self.virtual_machine.name, "name", for_update=True)

	def set_routes(self, value: Any) -> dict[str, Any]:
		"""Replace the complete route list. Metal owns the routes."""
		self.lock_network()
		return self.update_network({"routes": [route.as_dict() for route in self.validate_routes(value)]})

	def validate_routes(self, value: Any) -> tuple[Route, ...]:
		"""Parse a route list. An attached public IPv6 address owns the IPv6 Internet route."""
		try:
			routes = parse_routes(value)
		except ValueError as error:
			frappe.throw(_(str(error)), exc=AtlasUserError)
			raise AssertionError from error

		if self.get_ipv4_address_name() and _find_route(routes, IPV4_INTERNET_DESTINATION) != Route(
			IPV4_INTERNET_DESTINATION, ROUTE_VIA_HOST
		):
			frappe.throw(
				_("Detach the public IPv4 address before you remove its IPv4 route via host."),
				exc=AtlasUserError,
			)
		if self.virtual_machine.is_network_gateway and any(not route.is_via_host for route in routes):
			frappe.throw(
				_("A network gateway VM reaches other destinations only through its host."),
				exc=AtlasUserError,
			)
		if self.get_public_ipv6() and _find_route(routes, IPV6_INTERNET_DESTINATION) != _find_route(
			self.get_routes(), IPV6_INTERNET_DESTINATION
		):
			frappe.throw(
				_("Detach the public IPv6 address before you change the {0} route.").format(
					IPV6_INTERNET_DESTINATION
				),
				exc=AtlasUserError,
			)
		return routes

	def get_routes(self) -> list[Route]:
		"""Read current Metal routes so an edit cannot replace pending state."""
		return list(self.require_information().desired.network.routes)

	def has_gateway_routes(self) -> bool:
		return any(not route.is_via_host for route in self.get_routes())

	def get_routes_with(self, route: Route) -> list[dict[str, str]]:
		routes = [current for current in self.get_routes() if current.destination != route.destination]
		return [current.as_dict() for current in [*routes, route]]

	def get_routes_without(self, destination: str) -> list[dict[str, str]]:
		return [route.as_dict() for route in self.get_routes() if route.destination != destination]

	def get_route_editor_rows(self) -> list[dict[str, str]]:
		"""Add known gateway VM names for the route editor."""
		gateway_names = {
			get_virtual_machine_mesh_address(gateway): gateway.name
			for gateway in frappe.get_all(
				"Virtual Machine", filters={"is_network_gateway": 1}, fields=["name", "tenant_id"]
			)
		}
		return [
			route.as_dict() | {"gateway_virtual_machine": gateway_names.get(route.via, "")}
			for route in self.get_routes()
		]

	def get_public_ipv6(self) -> str:
		"""Return the public IPv6 prefix that Atlas assigned to this VM."""
		return (
			frappe.db.get_value(
				"Public IP Allocation",
				{"virtual_machine": self.virtual_machine.name, "version": "6"},
				"prefix",
			)
			or ""
		)

	def release_public_addresses(self) -> None:
		"""Set a detach intent for every public allocation of a VM that is going away."""
		for name in frappe.get_all(
			"Public IP Allocation", filters={"virtual_machine": self.virtual_machine.name}, pluck="name"
		):
			frappe.get_doc("Public IP Allocation", name).begin_detach()

	def get_ipv4_address_name(self) -> str | None:
		"""Return the public IPv4 allocation this virtual machine holds."""
		return frappe.db.get_value(
			"Public IP Allocation", {"virtual_machine": self.virtual_machine.name, "version": "4"}
		)

	def require_information(self) -> MetalVirtualMachine:
		"""Return Metal state or raise a Frappe error."""
		return self.perform_metal_operation(
			lambda metal_client: metal_client.get_virtual_machine(cast(str, self.virtual_machine.name))
		)

	def get_console_connection(self, mode: str) -> dict[str, str]:
		"""Return the Metal console connection values."""
		return self.perform_metal_operation(
			lambda metal_client: metal_client.get_console_connection(
				cast(str, self.virtual_machine.name), mode
			)
		)

	def perform_metal_operation[Result](self, operation: Callable[[MetalClient], Result]) -> Result:
		"""Run one Metal operation and translate its error for Frappe."""
		try:
			return operation(self.metal_client)
		except MetalClientError as error:
			self.raise_metal_error(error)

	@property
	def metal_client(self) -> MetalClient:
		"""Return a Metal client for the assigned Server."""
		server = cast("MetalServer", frappe.get_doc("Metal Server", self.virtual_machine.server))
		return MetalClient(server)

	@staticmethod
	def raise_metal_error(error: MetalClientError) -> Never:
		"""Raise one safe Metal failure at the Frappe boundary."""
		if error.is_insufficient_capacity:
			frappe.throw(
				_("The host has no room for this change. Stop the Virtual Machine and resize it to move it."),
				exc=InsufficientHostCapacity,
			)
		frappe.throw(_("Metal request failed: {0}").format(error), exc=MetalOperationError)
		raise AssertionError from error


def _find_route(routes: list[Route] | tuple[Route, ...], destination: str) -> Route | None:
	return next((route for route in routes if route.destination == destination), None)
