from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from frappe.utils import get_datetime, get_system_timezone
from pydantic import BaseModel, ConfigDict, Field

from atlas.api.core.base import PatchPayload, StrictModel
from atlas.vm.core.models import VirtualMachineCreateRequest

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address import (
		MetalServerIPAddress,
	)
	from atlas.vm.core.metal_models import MetalVirtualMachine
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

EgressMode = Literal["uplink", "mesh", "none"]


def to_unix_timestamp(value: str | datetime) -> int:
	"""Convert one API time value to Unix seconds."""
	moment = get_datetime(value)
	if not isinstance(moment, datetime):
		raise ValueError("The timestamp is not valid.")
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return int(moment.timestamp())


class JSONWebKey(BaseModel):
	"""One public Ed25519 signature key."""

	kty: Literal["OKP"]
	crv: Literal["Ed25519"]
	x: str
	kid: str
	alg: Literal["EdDSA"]
	use: Literal["sig"]
	key_ops: list[Literal["verify"]] = Field(default_factory=lambda: ["verify"])


class JSONWebKeySetResponse(BaseModel):
	"""The public keys that this Atlas region trusts."""

	keys: list[JSONWebKey]


class ReserveIPAddressPayload(StrictModel):
	"""Select the source of an IP address reservation."""

	source: Literal["pool", "provider"] = "pool"


class IPAddressResponse(BaseModel):
	"""A tenant public IPv4 address."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "203.0.113.10",
					"tenant_id": 7,
					"address": "203.0.113.10",
					"state": "reserved",
					"virtual_machine_id": None,
					"created_at": 1788834165,
				}
			]
		}
	)

	id: str
	tenant_id: int
	address: str
	state: str
	virtual_machine_id: str | None
	created_at: int

	@classmethod
	def from_document(cls, ip_address: MetalServerIPAddress) -> IPAddressResponse:
		"""Build a response from an IP address document or query row."""
		state = "reserved" if ip_address.status == "Allocated" else ip_address.status.lower()
		return cls(
			id=ip_address.name,
			tenant_id=ip_address.tenant_id,
			address=ip_address.address,
			state=state,
			virtual_machine_id=ip_address.virtual_machine or None,
			created_at=to_unix_timestamp(ip_address.creation),
		)


class ImageResponse(BaseModel):
	"""A tenant virtual machine image."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "8f1c2d3e4b5a6978",
					"tenant_id": 7,
					"title": "Worker snapshot",
					"image_type": "machine",
					"platform": "amd64",
					"operating_system": "Ubuntu",
					"operating_system_version": "24.04",
					"status": "available",
					"enabled": True,
					"supports_cloud_init": True,
					"cache_image": False,
					"memory_snapshot": False,
					"rootfs_size_mib": 20480,
					"kernel_size_mib": 8,
					"transfer_progress": 100,
					"transfer_error": None,
					"created_at": 1788834165,
				}
			]
		}
	)

	id: str
	tenant_id: int
	title: str
	image_type: str
	platform: str
	operating_system: str
	operating_system_version: str
	status: str
	enabled: bool
	supports_cloud_init: bool
	cache_image: bool
	memory_snapshot: bool
	rootfs_size_mib: int
	kernel_size_mib: int
	transfer_progress: int
	transfer_error: str | None
	created_at: int

	@classmethod
	def from_document(cls, image: VirtualMachineImage) -> ImageResponse:
		"""Build a response from an image document or query row."""
		return cls(
			id=image.name,
			tenant_id=image.tenant_id,
			title=image.title,
			image_type=image.image_type,
			platform=image.platform,
			operating_system=image.operating_system,
			operating_system_version=image.operating_system_version,
			status=image.status.lower(),
			enabled=bool(image.enabled),
			supports_cloud_init=bool(image.supports_cloud_init),
			cache_image=bool(image.cache_image),
			memory_snapshot=bool(image.memory_snapshot),
			rootfs_size_mib=image.image_size_mib,
			kernel_size_mib=image.kernel_size_mib,
			transfer_progress=image.transfer_progress,
			transfer_error=image.transfer_error or None,
			created_at=to_unix_timestamp(image.creation),
		)


class ImageDownloadQuery(StrictModel):
	"""Select one image artifact to download."""

	artifact: Literal["rootfs", "kernel"]


class ImageDownloadResponse(BaseModel):
	"""Signed downloads for one image."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"artifact": "rootfs",
					"url": "https://storage.example/rootfs",
					"size_mib": 20480,
					"sha256": "a" * 64,
					"expires_in": 86400,
					"expires_at": 1788920565,
				}
			]
		}
	)

	artifact: Literal["rootfs", "kernel"]
	url: str
	size_mib: int
	sha256: str
	expires_in: int
	expires_at: int

	@classmethod
	def from_download(cls, download: dict[str, Any]) -> ImageDownloadResponse:
		"""Build a response from signed image download values."""
		return cls(
			artifact=download["artifact"],
			url=download["url"],
			size_mib=download["size_mib"],
			sha256=download["sha256"],
			expires_in=download["expires_in"],
			expires_at=to_unix_timestamp(download["expires_at"]),
		)


class CreateVirtualMachinePayload(StrictModel):
	"""Values that create one virtual machine."""

	image_id: str = Field(min_length=1)
	vcpus: int = Field(gt=0)
	memory_mib: int = Field(gt=0)
	disk_mib: int = Field(gt=0)
	hostname: str = ""
	ssh_keys: list[str] = Field(default_factory=list)
	user_data: str = ""
	metadata: dict[str, str] = Field(default_factory=dict)
	ip_address_id: str | None = None
	egress: EgressMode = "uplink"
	is_privileged: bool = False
	sleep_after_idle_seconds: int = Field(default=0, ge=0, le=9_223_372_036)
	disk_throughput_mibps: int = Field(default=0, ge=0)
	disk_iops: int = Field(default=0, ge=0)
	private_network_throughput_mibps: int = Field(default=0, ge=0)
	public_network_throughput_mibps: int = Field(default=0, ge=0)

	def to_domain_request(
		self, tenant_id: int, image_name: str, ip_address_name: str | None
	) -> VirtualMachineCreateRequest:
		"""Build the domain request for this API payload."""
		return VirtualMachineCreateRequest(
			virtual_machine_image=image_name,
			virtual_cpu_count=self.vcpus,
			memory_mib=self.memory_mib,
			disk_mib=self.disk_mib,
			tenant_id=tenant_id,
			hostname=self.hostname,
			ssh_keys=tuple(self.ssh_keys),
			user_data=self.user_data,
			metadata=self.metadata,
			egress=self.egress,
			is_privileged=self.is_privileged,
			sleep_after_idle_seconds=self.sleep_after_idle_seconds,
			disk_throughput_mibps=self.disk_throughput_mibps,
			disk_iops=self.disk_iops,
			private_network_throughput_mibps=self.private_network_throughput_mibps,
			public_network_throughput_mibps=self.public_network_throughput_mibps,
			server_ip_address=ip_address_name,
		)


class ComputeUpdatePayload(PatchPayload):
	"""New compute configuration."""

	vcpus: int | None = Field(default=None, gt=0)
	memory_mib: int | None = Field(default=None, gt=0)
	sleep_after_idle_seconds: int | None = Field(default=None, ge=0, le=9_223_372_036)

	def to_domain_changes(self) -> dict[str, Any]:
		"""Return the field names that the VM service accepts."""
		changes = self.model_dump(exclude_none=True)
		if "vcpus" in changes:
			changes["virtual_cpu_count"] = changes.pop("vcpus")
		return changes


class DiskUpdatePayload(PatchPayload):
	"""New disk size and disk rate limits."""

	disk_mib: int | None = Field(default=None, gt=0)
	disk_throughput_mibps: int | None = Field(default=None, ge=0)
	disk_iops: int | None = Field(default=None, ge=0)

	def to_domain_changes(self) -> dict[str, int]:
		"""Return the field names that the VM service accepts."""
		changes = self.model_dump(exclude_none=True)
		if "disk_mib" in changes:
			changes["size_mib"] = changes.pop("disk_mib")
		return changes


class NetworkUpdatePayload(PatchPayload):
	"""New egress mode and network rate limits."""

	egress: EgressMode | None = None
	private_network_throughput_mibps: int | None = Field(default=None, ge=0)
	public_network_throughput_mibps: int | None = Field(default=None, ge=0)


class SSHKeysReplacementPayload(StrictModel):
	"""The complete authorized key list."""

	ssh_keys: list[str]


class MetadataReplacementPayload(StrictModel):
	"""The complete custom metadata map."""

	metadata: dict[str, str]


class IPAddressAssignmentPayload(StrictModel):
	"""The public IPv4 address to attach."""

	ip_address_id: str = Field(min_length=1)


class SnapshotPayload(StrictModel):
	"""Values that create one image from a virtual machine."""

	title: str = Field(min_length=1)
	image_type: Literal["machine", "system"] = "machine"
	cache_image: bool = False
	memory_snapshot: bool = False


class ConsoleTokenPayload(StrictModel):
	"""The console mode that the token opens."""

	mode: Literal["tty", "ssh"] = "tty"


def get_current_state(virtual_machine: VirtualMachine, reported_state: str | None) -> str:
	"""Return the state a caller sees. An Atlas transition hides the host state."""
	if virtual_machine.is_draft:
		return "pending"
	if virtual_machine.is_terminating:
		return "terminating"
	return reported_state or "unknown"


class VirtualMachineResponse(BaseModel):
	"""A stored tenant virtual machine."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "vm-00001",
					"tenant_id": 7,
					"image_id": "8f1c2d3e4b5a6978",
					"vcpus": 2,
					"memory_mib": 2048,
					"disk_mib": 20480,
					"sleep_after_idle_seconds": 0,
					"created_at": 1788834165,
				}
			]
		}
	)

	id: str
	tenant_id: int
	image_id: str
	vcpus: int
	memory_mib: int
	disk_mib: int
	sleep_after_idle_seconds: int
	created_at: int

	@classmethod
	def from_document(cls, virtual_machine: VirtualMachine) -> VirtualMachineResponse:
		"""Build a response from a virtual machine document or query row."""
		return cls(
			id=virtual_machine.name,
			tenant_id=virtual_machine.tenant_id,
			image_id=virtual_machine.virtual_machine_image,
			vcpus=virtual_machine.vcpus,
			memory_mib=virtual_machine.memory_mib,
			disk_mib=virtual_machine.disk_mib,
			sleep_after_idle_seconds=virtual_machine.sleep_after_idle_seconds,
			created_at=to_unix_timestamp(virtual_machine.creation),
		)


class VirtualMachineListResponse(VirtualMachineResponse):
	"""A stored virtual machine with its last known host state."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					**VirtualMachineResponse.model_config["json_schema_extra"]["examples"][0],
					"last_known_state": "running",
					"state_synced_at": 1788834165,
				}
			]
		}
	)

	last_known_state: str
	state_synced_at: int | None

	@classmethod
	def from_document_and_state(
		cls, virtual_machine: VirtualMachine, state: Any | None
	) -> VirtualMachineListResponse:
		"""Build a list response from Atlas and the stored host state."""
		stored = VirtualMachineResponse.from_document(virtual_machine)
		return cls(
			**stored.model_dump(),
			last_known_state=get_current_state(virtual_machine, state.status if state else None),
			state_synced_at=to_unix_timestamp(state.synced_at) if state else None,
		)


class VirtualMachineCompute(BaseModel):
	"""The compute shape of one virtual machine."""

	vcpus: int
	memory_mib: int
	sleep_after_idle_seconds: int


class VirtualMachineDisk(BaseModel):
	"""The disk size and its rate limits."""

	size_mib: int
	throughput_mibps: int
	iops: int
	used_mib: int | None


class VirtualMachineNetwork(BaseModel):
	"""The addresses and network limits of one virtual machine."""

	egress: str | None
	public_ipv4: str | None
	mesh_ipv6: str | None
	mac: str | None
	private_network_throughput_mibps: int
	public_network_throughput_mibps: int


class VirtualMachineGuest(BaseModel):
	"""The guest configuration of one virtual machine."""

	hostname: str | None
	ssh_keys: list[str]
	metadata: dict[str, str]


class VirtualMachineDetailResponse(BaseModel):
	"""One virtual machine with its state, addresses, and guest configuration."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "vm-00001",
					"tenant_id": 7,
					"image_id": "8f1c2d3e4b5a6978",
					"created_at": 1788834165,
					"is_privileged": False,
					"desired_state": "running",
					"current_state": "running",
					"error": None,
					"compute": {"vcpus": 2, "memory_mib": 2048, "sleep_after_idle_seconds": 0},
					"disk": {"size_mib": 20480, "throughput_mibps": 0, "iops": 0, "used_mib": 8123},
					"network": {
						"egress": "uplink",
						"public_ipv4": "203.0.113.10",
						"mesh_ipv6": "fdaa:1::5",
						"mac": "52:54:00:12:34:56",
						"private_network_throughput_mibps": 0,
						"public_network_throughput_mibps": 0,
					},
					"guest": {
						"hostname": "worker-1",
						"ssh_keys": ["ssh-ed25519 AAAA"],
						"metadata": {"role": "worker"},
					},
				}
			]
		}
	)

	id: str
	tenant_id: int
	image_id: str
	created_at: int
	is_privileged: bool
	desired_state: str | None
	current_state: str
	error: str | None
	compute: VirtualMachineCompute
	disk: VirtualMachineDisk
	network: VirtualMachineNetwork
	guest: VirtualMachineGuest

	@classmethod
	def from_document_and_metal(
		cls, virtual_machine: VirtualMachine, information: MetalVirtualMachine | None
	) -> VirtualMachineDetailResponse:
		"""Build a detailed response from Atlas storage and the live Metal state."""
		desired = information.desired if information else None
		observed = information.observed if information else None
		return cls(
			id=virtual_machine.name,
			tenant_id=virtual_machine.tenant_id,
			image_id=virtual_machine.virtual_machine_image,
			created_at=to_unix_timestamp(virtual_machine.creation),
			is_privileged=bool(virtual_machine.is_privileged),
			desired_state=desired.state if desired else None,
			current_state=get_current_state(virtual_machine, observed.state if observed else None),
			error=observed.error.message if observed and observed.error else None,
			compute=VirtualMachineCompute(
				vcpus=virtual_machine.vcpus,
				memory_mib=virtual_machine.memory_mib,
				sleep_after_idle_seconds=virtual_machine.sleep_after_idle_seconds,
			),
			disk=VirtualMachineDisk(
				size_mib=virtual_machine.disk_mib,
				throughput_mibps=desired.disk.throughput_mibps if desired else 0,
				iops=desired.disk.iops if desired else 0,
				used_mib=observed.disk.used_mib if observed else None,
			),
			network=VirtualMachineNetwork(
				egress=desired.network.egress if desired else None,
				public_ipv4=desired.network.public_ipv4 or None if desired else None,
				mesh_ipv6=desired.network.wireguard_mesh_ipv6 or None if desired else None,
				mac=observed.network.mac or None if observed else None,
				private_network_throughput_mibps=(
					desired.network.private_network_throughput_mibps if desired else 0
				),
				public_network_throughput_mibps=(
					desired.network.public_network_throughput_mibps if desired else 0
				),
			),
			guest=VirtualMachineGuest(
				hostname=desired.guest.hostname or None if desired else None,
				ssh_keys=list(desired.guest.ssh_keys) if desired else [],
				metadata=dict(desired.guest.metadata) if desired else {},
			),
		)


class ConsoleTokenResponse(BaseModel):
	"""A single-use console token."""

	model_config = ConfigDict(
		json_schema_extra={"examples": [{"token": "console-token", "mode": "tty", "expires_in": 60}]}
	)

	token: str
	mode: Literal["tty", "ssh"]
	expires_in: int
