from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from atlas.vm.core.metal_client import MetalClient, MetalClientError, throw_metal_error

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.server.doctype.server.server import Server
	from atlas.server.doctype.server_ip_address.server_ip_address import ServerIPAddress
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


EGRESS_MODES = ("uplink", "mesh", "none")


@dataclass(frozen=True, slots=True)
class VirtualMachineCreateRequest:
	"""Store the validated values for one VM request."""

	virtual_machine_image: str
	vcpus: int
	memory_mib: int
	disk_mib: int
	tenant_id: int
	hostname: str = ""
	ssh_keys: tuple[str, ...] = ()
	user_data: str = ""
	egress: str = "uplink"
	disk_throughput_mibps: int = 0
	disk_iops: int = 0
	private_network_throughput_mibps: int = 0
	public_network_throughput_mibps: int = 0
	server_ip_address: str | None = None
	metadata: dict[str, str] = field(default_factory=dict)

	@classmethod
	def from_value(cls, value: str | dict[str, Any]) -> "VirtualMachineCreateRequest":
		payload = frappe.parse_json(value) if isinstance(value, str) else value
		if not isinstance(payload, dict):
			raise ValueError("Virtual Machine request must be a JSON object.")

		image = payload.get("virtual_machine_image")
		if not isinstance(image, str) or not image:
			raise ValueError("Virtual Machine Image is required.")

		vcpus = cls._positive_integer(payload, "vcpus", "vCPUs")
		memory_mib = cls._positive_integer(payload, "memory_mib", "Memory")
		disk_mib = cls._positive_integer(payload, "disk_mib", "Disk")
		tenant_id = payload.get("tenant_id")

		if not isinstance(tenant_id, int) or isinstance(tenant_id, bool) or not 0 <= tenant_id <= 0xFFFFFFFF:
			raise ValueError("Tenant ID must be a 32-bit unsigned integer.")

		egress = payload.get("egress") or "uplink"
		if egress not in EGRESS_MODES:
			raise ValueError("Egress must be uplink, mesh, or none.")

		public_throughput = cls._throughput(payload, "public_network_throughput_mibps")
		server_ip_address = payload.get("server_ip_address") or None
		cls._validate_internet_path(egress, server_ip_address)

		return cls(
			virtual_machine_image=image,
			vcpus=vcpus,
			memory_mib=memory_mib,
			disk_mib=disk_mib,
			tenant_id=tenant_id,
			hostname=str(payload.get("hostname") or ""),
			ssh_keys=tuple(str(payload.get("ssh_keys") or "").splitlines()),
			user_data=str(payload.get("user_data") or ""),
			egress=egress,
			disk_throughput_mibps=cls._throughput(payload, "disk_throughput_mibps"),
			disk_iops=cls._throughput(payload, "disk_iops"),
			private_network_throughput_mibps=cls._throughput(payload, "private_network_throughput_mibps"),
			public_network_throughput_mibps=public_throughput,
			server_ip_address=server_ip_address,
			metadata=cls._metadata(payload),
		)

	@staticmethod
	def _validate_internet_path(egress: str, server_ip_address: str | None) -> None:
		"""Reject a public IPv4 address when the mode has no internet path."""
		if egress != "uplink" and server_ip_address:
			raise ValueError("A public IPv4 address requires uplink egress.")

	@staticmethod
	def _throughput(payload: dict[str, Any], field: str) -> int:
		"""Return one throughput limit in MiB/s. A value of 0 does not apply a limit."""
		value = payload.get(field) or 0
		if not isinstance(value, int) or isinstance(value, bool) or value < 0:
			raise ValueError(f"{field} must be a non-negative integer.")
		return value

	@staticmethod
	def _metadata(payload: dict[str, Any]) -> dict[str, str]:
		raw = payload.get("metadata") or {}
		if not isinstance(raw, dict):
			raise ValueError("Metadata must be a string-to-string map.")
		metadata: dict[str, str] = {}
		for key, value in raw.items():
			if not isinstance(key, str) or not isinstance(value, str):
				raise ValueError("Metadata keys and values must be strings.")
			key = key.strip()
			if not key:
				raise ValueError("Metadata key cannot be empty.")
			metadata[key] = value
		return metadata

	@staticmethod
	def _positive_integer(payload: dict[str, Any], field: str, label: str) -> int:
		value = payload.get(field)
		if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
			raise ValueError(f"{label} must be a positive integer.")
		return value


@dataclass(frozen=True, slots=True)
class PlacementCapacity:
	"""Store one host capacity sample for placement."""

	server: str
	architecture: str
	available_cpu_count: int
	available_memory_mib: int
	available_storage_mib: int

	def can_host(self, request: VirtualMachineCreateRequest, architecture: str) -> bool:
		return (
			self.architecture == architecture
			and self.available_cpu_count >= request.vcpus
			and self.available_memory_mib >= request.memory_mib
			and self.available_storage_mib >= request.disk_mib
		)


class VirtualMachineManager:
	"""Create and reconcile Atlas VM request records."""

	def create(self, value: str | dict[str, Any]) -> dict[str, str | bool]:
		try:
			request = VirtualMachineCreateRequest.from_value(value)
		except ValueError as error:
			frappe.throw(_(str(error)))
			raise AssertionError from error

		image = self.get_image(request.virtual_machine_image)
		image.validate_compatibility(request.disk_mib)
		server = self.select_server(request, image.platform)

		image_name = cast(str, image.name)
		server_name = cast(str, server.name)
		virtual_machine = self.insert_draft(request, image_name, server_name)
		virtual_machine_name = cast(str, virtual_machine.name)
		server_ip_address = (
			virtual_machine.assign_ip_address(request.server_ip_address)
			if request.server_ip_address
			else None
		)
		metal_request = self.get_metal_request(request, image, virtual_machine, server_ip_address)
		frappe.db.commit()  # nosemgrep

		try:
			MetalClient(server).put_virtual_machine(virtual_machine_name, metal_request)
		except MetalClientError as error:
			if error.uncertain:
				return {"name": virtual_machine_name, "is_draft": True}
			throw_metal_error(error)

		virtual_machine.is_draft = 0
		virtual_machine.save(ignore_permissions=True)
		return {"name": virtual_machine_name, "is_draft": False}

	def get_image(self, image_name: str) -> "VirtualMachineImage":
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		if not image.enabled:
			frappe.throw(_("Virtual Machine Image {0} is disabled.").format(image.title))
		image.validate_is_available()
		return image

	def select_server(self, request: VirtualMachineCreateRequest, architecture: str) -> "Server":
		servers = frappe.get_all(
			"Server",
			filters={"status": "Running", "is_provisioning_completed": 1},
			fields=["name", "architecture"],
		)
		if not servers:
			frappe.throw(_("No running Server is ready for Virtual Machines."))
		architecture_by_server = {server.name: server.architecture for server in servers}

		freshness_cutoff = add_to_date(now_datetime(), minutes=-2)
		usage_rows = frappe.get_all(
			"Server Usage",
			filters={
				"server": ["in", list(architecture_by_server)],
				"creation": [">=", freshness_cutoff],
			},
			fields=[
				"server",
				"available_cpu_count",
				"available_memory_mib",
				"available_storage_mib",
			],
			order_by="creation desc",
		)
		latest_by_server: dict[str, PlacementCapacity] = {}
		for usage_row in usage_rows:
			if usage_row.server not in latest_by_server:
				latest_by_server[usage_row.server] = PlacementCapacity(
					server=usage_row.server,
					architecture=architecture_by_server[usage_row.server],
					available_cpu_count=usage_row.available_cpu_count,
					available_memory_mib=usage_row.available_memory_mib,
					available_storage_mib=usage_row.available_storage_mib,
				)

		candidates = [
			capacity for capacity in latest_by_server.values() if capacity.can_host(request, architecture)
		]
		if not candidates:
			frappe.throw(_("No Server has current capacity for this Virtual Machine."))
		selected_capacity = max(
			candidates,
			key=lambda capacity: (
				capacity.available_memory_mib,
				capacity.available_cpu_count,
				capacity.available_storage_mib,
				capacity.server,
			),
		)
		return cast("Server", frappe.get_doc("Server", selected_capacity.server))

	def insert_draft(self, request: VirtualMachineCreateRequest, image: str, server: str) -> "VirtualMachine":
		virtual_machine = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"is_draft": 1,
				"server": server,
				"virtual_machine_image": image,
				"vcpus": request.vcpus,
				"memory_mib": request.memory_mib,
				"disk_mib": request.disk_mib,
				"tenant_id": request.tenant_id,
			}
		)
		virtual_machine.flags.created_by_virtual_machine_api = True
		return cast("VirtualMachine", virtual_machine.insert(ignore_permissions=True))

	def get_metal_request(
		self,
		request: VirtualMachineCreateRequest,
		image: "VirtualMachineImage",
		virtual_machine: Document,
		server_ip_address: "ServerIPAddress | None",
	) -> dict[str, Any]:
		return {
			"vcpus": request.vcpus,
			"memory_mib": request.memory_mib,
			"disk_mib": request.disk_mib,
			"image": image.get_metal_image_request(request.user_data),
			"hostname": request.hostname,
			"ssh_keys": list(request.ssh_keys),
			"user_data": request.user_data,
			"metadata": dict(request.metadata),
			"disk": {
				"throughput_mibps": request.disk_throughput_mibps,
				"iops": request.disk_iops,
			},
			"network": {
				"public_ipv4": server_ip_address.address if server_ip_address else None,
				"wireguard_mesh_ipv6": self.get_wireguard_mesh_ipv6(virtual_machine),
				"private_network_throughput_mibps": request.private_network_throughput_mibps,
				"public_network_throughput_mibps": request.public_network_throughput_mibps,
				"egress": request.egress,
			},
		}

	@staticmethod
	def get_wireguard_mesh_ipv6(virtual_machine: Document) -> str:
		"""Return the mesh address from stable Atlas request metadata."""
		settings = cast("AtlasSettings", frappe.get_single("Atlas Settings"))
		region_id = settings.region_id
		if not 0 <= region_id <= 0xFFFF:
			frappe.throw(_("Atlas Settings region ID must be a 16-bit unsigned integer."))
		virtual_machine_name = cast(str, virtual_machine.name)
		virtual_machine_number = int(virtual_machine_name.rsplit("-", 1)[-1])
		if virtual_machine_number > 0xFFFFFFFF:
			frappe.throw(
				_("Virtual Machine number {0} is too large for a mesh address.").format(
					virtual_machine_number
				)
			)
		address = (
			(0xFDAA << 112) | (region_id << 96) | (virtual_machine.tenant_id << 64) | virtual_machine_number
		)
		return str(ipaddress.IPv6Address(address))
