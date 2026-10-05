from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone
from pydantic import (
	AnyHttpUrl,
	AwareDatetime,
	BaseModel,
	ConfigDict,
	Discriminator,
	Field,
	StringConstraints,
	Tag,
	model_validator,
)

from atlas.api.core.base import ListQuery, PatchPayload, StrictModel
from atlas.api.core.errors import ApiErrorField
from atlas.atlas.core.tags import (
	MAXIMUM_TAG_KEY_LENGTH,
	MAXIMUM_TAG_VALUE_LENGTH,
	MAXIMUM_TAGS,
	read_tags,
	read_tags_for,
)
from atlas.vm.core.models import (
	DEFAULT_ROUTES,
	IPV4_INTERNET_DESTINATION,
	MAXIMUM_CPU_MILLICORES,
	MAXIMUM_FIREWALL_PREFIXES,
	MINIMUM_CPU_MILLICORES,
	FirewallConfiguration,
	FirewallRule,
	VirtualMachineCreateRequest,
)
from atlas.vm.core.placement.affinity import AffinityOperator, AffinityResource, AffinityRules

if TYPE_CHECKING:
	from atlas.metal_server.doctype.public_ip_allocation.public_ip_allocation import (
		PublicIPAllocation,
	)
	from atlas.vm.core.metal_models import MetalVirtualMachine, MetalVirtualMachineMetrics
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

TagMap = Annotated[
	dict[
		Annotated[str, StringConstraints(max_length=MAXIMUM_TAG_KEY_LENGTH)],
		Annotated[str, StringConstraints(max_length=MAXIMUM_TAG_VALUE_LENGTH)],
	],
	Field(max_length=MAXIMUM_TAGS, description="Resource tags as key-value pairs."),
]
FirewallProtocol = Literal["any", "tcp", "udp", "icmp"]
Architecture = Literal["amd64", "arm64"]
ImageType = Literal["system", "machine"]
PublicIPStatus = Literal["available", "reserved", "attaching", "attached", "detaching"]
ImageStatus = Literal[
	"pending",
	"snapshotting",
	"uploading",
	"completing",
	"cleaning",
	"available",
	"failed",
	"deleting",
	"archived",
]
AUTO_IP_ALLOCATION = "auto"


class OutOfCapacityError(BaseModel):
	"""No host can accept the VM. The fleet needs more capacity."""

	code: Literal["out_of_capacity"] = Field(description="Stable machine-readable error code.")
	message: str = Field(description="Safe description of the failure.")
	fields: list[ApiErrorField] = Field(description="Invalid request fields, or an empty list.")


class PlacementBusyError(BaseModel):
	"""Every candidate host was held by another placement.

	The fleet has room. Retry at once, guided by the `Retry-After` header.
	"""

	code: Literal["placement_busy"] = Field(description="Stable machine-readable error code.")
	message: str = Field(description="Safe description of the failure.")
	fields: list[ApiErrorField] = Field(description="Invalid request fields, or an empty list.")


class AffinityUnsatisfiedError(BaseModel):
	"""No host with room meets the affinity rules of the VM."""

	code: Literal["affinity_unsatisfied"] = Field(description="Stable machine-readable error code.")
	message: str = Field(description="Safe description of the failure.")
	fields: list[ApiErrorField] = Field(description="Invalid request fields, or an empty list.")


CapacityError = Annotated[
	OutOfCapacityError | PlacementBusyError | AffinityUnsatisfiedError,
	Field(discriminator="code"),
]


class CapacityUnavailableResponse(BaseModel):
	"""The JSON body of a 503 from virtual machine creation.

	`error.code` separates a fleet that is full from one that is only busy, so a
	caller can retry a busy placement at once and escalate a full one.
	"""

	error: CapacityError = Field(description="Capacity failure details.")


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

	kty: Literal["OKP"] = Field(description="JSON Web Key type.")
	crv: Literal["Ed25519"] = Field(description="Edwards curve name.")
	x: str = Field(description="Base64url-encoded public key.")
	kid: str = Field(description="Key ID used to select this key.")
	alg: Literal["EdDSA"] = Field(description="Signing algorithm.")
	use: Literal["sig"] = Field(description="Intended key use.")
	key_ops: list[Literal["verify"]] = Field(
		default_factory=lambda: ["verify"], description="Operations allowed for this public key."
	)


class JSONWebKeySetResponse(BaseModel):
	"""The public keys that this Atlas region trusts."""

	keys: list[JSONWebKey] = Field(description="Active public signature keys.")


class ConfigureWebhooksPayload(StrictModel):
	"""The destination of the event deliveries of one Central."""

	request_url: AnyHttpUrl = Field(description="HTTP or HTTPS URL that receives every delivery.")
	webhook_secret: str = Field(min_length=1, description="Shared secret that signs every delivery.")
	central_id: int = Field(default=1, ge=1, description="Receiving Central.")
	enabled: bool = Field(default=True, description="Whether Atlas sends webhook events.")

	@model_validator(mode="after")
	def validate_central_id(self) -> ConfigureWebhooksPayload:
		"""Restrict extra Central deliveries to development or an explicit site setting."""
		if self.central_id == 1:
			return self

		allows_multiple = (
			frappe.conf.get("developer_mode") == 1 or frappe.conf.get("allow_multiple_central_webhooks") == 1
		)
		if not allows_multiple:
			raise ValueError("central_id must be 1 unless multiple Central webhooks are enabled.")

		return self


class WebhookConfigurationResponse(BaseModel):
	"""The configured event deliveries of one Central."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"central_id": 1,
					"enabled": True,
					"webhooks": [
						"Virtual Machine State - On Update - Central - 1",
						"Virtual Machine State - On Trash - Central - 1",
					],
				}
			]
		}
	)

	central_id: int = Field(description="Receiving Central.")
	enabled: bool = Field(description="Whether Atlas sends webhook events.")
	webhooks: list[str] = Field(description="Frappe webhook records managed for this Central.")


class PublicIPListQuery(ListQuery):
	"""Page through public IPs, and narrow them to one IP version when asked."""

	version: Literal["4", "6"] | None = Field(
		default=None, description="Return only this IP protocol version."
	)


class ReservePublicIPPayload(StrictModel):
	"""Select the IP version of one direct reservation."""

	version: Literal[4, 6] = Field(description="IP protocol version to reserve.")


class PublicIPResponse(BaseModel):
	"""One tenant public IP."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "32eb57bc-9548-4a89-8358-543e26883569",
					"tenant_id": 7,
					"prefix": "203.0.113.10/32",
					"version": 4,
					"status": "reserved",
					"reserved": True,
					"delivery": "direct",
					"virtual_machine_id": None,
					"tags": {"pool": "edge"},
					"created_at": 1788834165,
				}
			]
		}
	)

	id: str = Field(description="Public IP allocation ID.")
	tenant_id: int = Field(description="Tenant that owns the allocation.")
	prefix: str = Field(description="Allocated address or network in CIDR notation.")
	version: Literal[4, 6] = Field(description="IP protocol version.")
	status: PublicIPStatus = Field(description="Current allocation lifecycle state.")
	reserved: bool = Field(description="Whether the tenant keeps this allocation after detach.")
	delivery: Literal["direct", "routed"] = Field(description="How traffic reaches the virtual machine.")
	virtual_machine_id: str | None = Field(description="Attached virtual machine ID, or null when detached.")
	tags: dict[str, str] = Field(description="Resource tags as key-value pairs.")
	created_at: int = Field(ge=0, description="Creation time as Unix seconds.")

	@classmethod
	def from_document(
		cls, allocation: PublicIPAllocation, tags: dict[str, str] | None = None
	) -> PublicIPResponse:
		"""Build a response from an allocation document or query row."""
		gateway = getattr(allocation, "gateway", None)
		if gateway is None:
			gateway = frappe.db.get_value("Public IP Pool", allocation.pool, "gateway")
		return cls(
			id=allocation.name,
			tenant_id=allocation.tenant_id,
			prefix=allocation.prefix,
			version=int(allocation.version),
			status=allocation.status.lower(),
			reserved=bool(allocation.is_reserved),
			delivery="routed" if gateway else "direct",
			virtual_machine_id=allocation.virtual_machine or None,
			tags=read_tags(allocation) if tags is None else tags,
			created_at=to_unix_timestamp(allocation.creation),
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
					"architecture": "amd64",
					"status": "available",
					"enabled": True,
					"cache_image": False,
					"memory_snapshot": False,
					"is_termination_protected": False,
					"rootfs_size_mib": 20480,
					"kernel_size_mib": 8,
					"transfer_progress": 100,
					"transfer_error": None,
					"tags": {"os": "Ubuntu", "os_version": "24.04"},
					"created_at": 1788834165,
				}
			]
		}
	)

	id: str = Field(description="Virtual machine image ID.")
	tenant_id: int = Field(description="Tenant that owns the image.")
	title: str = Field(description="Display title.")
	image_type: ImageType = Field(description="System image or tenant machine snapshot.")
	architecture: Architecture = Field(description="CPU architecture.")
	status: ImageStatus = Field(description="Current image lifecycle state.")
	enabled: bool = Field(description="Whether the image can create a virtual machine.")
	cache_image: bool = Field(description="Whether hosts may keep this image cached.")
	memory_snapshot: bool = Field(description="Whether the image includes guest memory.")
	is_termination_protected: bool = Field(description="Whether deletion is blocked.")
	rootfs_size_mib: int = Field(ge=0, description="Root filesystem size in MiB.")
	kernel_size_mib: int = Field(ge=0, description="Kernel artifact size in MiB.")
	transfer_progress: int = Field(ge=0, le=100, description="Transfer completion percentage.")
	transfer_error: str | None = Field(description="Last transfer error, or null.")
	tags: dict[str, str] = Field(description="Resource tags as key-value pairs.")
	created_at: int = Field(ge=0, description="Creation time as Unix seconds.")

	@classmethod
	def from_document(cls, image: VirtualMachineImage, tags: dict[str, str] | None = None) -> ImageResponse:
		"""Build a response from an image document, or from a query row with its tags."""
		return cls(
			id=image.name,
			tenant_id=image.tenant_id,
			title=image.title,
			image_type=image.image_type,
			architecture=image.architecture,
			status=image.status.lower(),
			enabled=bool(image.enabled),
			cache_image=bool(image.cache_image),
			memory_snapshot=bool(image.memory_snapshot),
			is_termination_protected=bool(image.is_termination_protected),
			rootfs_size_mib=image.image_size_mib,
			kernel_size_mib=image.kernel_size_mib,
			transfer_progress=image.transfer_progress,
			transfer_error=image.transfer_error or None,
			tags=read_tags(image) if tags is None else tags,
			created_at=to_unix_timestamp(image.creation),
		)


class ImageListQuery(ListQuery):
	"""Page through images, and narrow them to one image type when asked."""

	image_type: ImageType | None = Field(default=None, description="Return only this image type.")


class ImageDownloadQuery(StrictModel):
	"""Select one image artifact to download."""

	artifact: Literal["rootfs", "kernel"] = Field(description="Image artifact to download.")


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

	artifact: Literal["rootfs", "kernel"] = Field(description="Image artifact represented by this URL.")
	url: AnyHttpUrl = Field(description="Temporary signed download URL.")
	size_mib: int = Field(ge=0, description="Artifact size in MiB.")
	sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase SHA-256 digest.")
	expires_in: int = Field(ge=0, description="Seconds until the URL expires.")
	expires_at: int = Field(ge=0, description="URL expiry time as Unix seconds.")

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


class FirewallRulePayload(StrictModel):
	"""One firewall allow rule."""

	protocol: FirewallProtocol = Field(description="Allowed IP protocol.")
	ports: str = Field(default="", description="Allowed ports or ranges. Leave empty for all ports.")
	cidrs: list[str] = Field(min_length=1, description="Source or destination networks in CIDR notation.")

	@model_validator(mode="after")
	def validate_rule(self) -> FirewallRulePayload:
		"""Apply the shared firewall rule validation."""
		FirewallRule.from_value(self.model_dump())
		return self


class FirewallPayload(StrictModel):
	"""The complete desired firewall configuration."""

	enabled: bool = Field(default=False, description="Whether the firewall enforces these rules.")
	inbound: list[FirewallRulePayload] = Field(default_factory=list, description="Inbound allow rules.")
	outbound: list[FirewallRulePayload] = Field(default_factory=list, description="Outbound allow rules.")

	@model_validator(mode="after")
	def validate_effective_rule_count(self) -> FirewallPayload:
		"""Limit the expanded CIDR count."""
		count = sum(len(rule.cidrs) for rule in (*self.inbound, *self.outbound))
		if count > MAXIMUM_FIREWALL_PREFIXES:
			raise ValueError(f"Firewall must not exceed {MAXIMUM_FIREWALL_PREFIXES} prefix entries.")
		return self


class FirewallUpdatePayload(PatchPayload):
	"""Selected firewall fields to replace."""

	enabled: bool | None = Field(default=None, description="New firewall enforcement state.")
	inbound: list[FirewallRulePayload] | None = Field(
		default=None, description="Complete inbound allow rule list."
	)
	outbound: list[FirewallRulePayload] | None = Field(
		default=None, description="Complete outbound allow rule list."
	)

	@model_validator(mode="after")
	def validate_effective_rule_count(self) -> FirewallUpdatePayload:
		"""Reject a partial list that exceeds the complete firewall limit."""
		rules = (self.inbound or []) + (self.outbound or [])
		count = sum(len(rule.cidrs) for rule in rules)
		if count > MAXIMUM_FIREWALL_PREFIXES:
			raise ValueError(f"Firewall must not exceed {MAXIMUM_FIREWALL_PREFIXES} prefix entries.")
		return self


class AffinityRulePayload(StrictModel):
	"""One rule on the tags of the candidate Metal Server, or of a VM that runs on it."""

	resource: AffinityResource = Field(
		description="`metal_server` checks the candidate host. `virtual_machine` checks the VMs on the candidate host."
	)
	operator: AffinityOperator = Field(
		description="`has` needs a resource with every tag pair. `has_not` rejects such a resource."
	)
	tags: TagMap = Field(min_length=1, description="Tag pairs that must all be on one resource.")
	within: Annotated[str, StringConstraints(min_length=1, max_length=MAXIMUM_TAG_KEY_LENGTH)] | None = Field(
		default=None,
		description="A host tag key, such as `rack`. A `virtual_machine` rule then reads the VMs on every host that has the same value for this key as the candidate host. A host without the key fails the rule.",
	)


class AffinityAnyOfPayload(StrictModel):
	"""A group that holds when at least one of its rules or groups holds."""

	any_of: list[AffinityNodePayload] = Field(min_length=1, description="Rules or groups. One must hold.")


class AffinityAllOfPayload(StrictModel):
	"""A group that holds when every one of its rules or groups holds."""

	all_of: list[AffinityNodePayload] = Field(
		min_length=1, description="Rules or groups. Every one must hold."
	)


def _affinity_node_kind(value: object) -> str | None:
	"""Pick the node model by its keys, so an invalid node reports the errors of one model."""
	if isinstance(value, BaseModel):
		keys = type(value).model_fields
	elif isinstance(value, dict):
		keys = value
	else:
		return None

	return next((kind for kind in ("any_of", "all_of") if kind in keys), "rule")


AffinityNodePayload = Annotated[
	Annotated[AffinityRulePayload, Tag("rule")]
	| Annotated[AffinityAnyOfPayload, Tag("any_of")]
	| Annotated[AffinityAllOfPayload, Tag("all_of")],
	Discriminator(_affinity_node_kind),
]
AffinityAnyOfPayload.model_rebuild()
AffinityAllOfPayload.model_rebuild()


class CreateVirtualMachinePayload(StrictModel):
	"""Values that create one virtual machine."""

	image_id: str = Field(min_length=1, description="Image used to create the virtual machine.")
	cpu_millicores: int = Field(
		ge=MINIMUM_CPU_MILLICORES,
		le=MAXIMUM_CPU_MILLICORES,
		description="CPU capacity in millicores. 1000 millicores equals one virtual CPU.",
	)
	memory_mib: int = Field(gt=0, description="Memory capacity in MiB.")
	disk_mib: int = Field(gt=0, description="Root disk capacity in MiB.")
	hostname: str = Field(default="", description="Guest hostname.")
	ssh_keys: list[str] = Field(default_factory=list, description="Authorized SSH public keys.")
	user_data: str = Field(default="", description="Cloud-init user data supplied to the guest.")
	metadata: dict[str, str] = Field(default_factory=dict, description="Custom guest metadata.")
	public_ipv4: str | None = Field(default=None, description="Reserved public IPv4 allocation ID, or null.")
	public_ipv6: str | None = Field(default=None, description="Reserved public IPv6 allocation ID, or null.")
	ipv4_internet_access: bool = Field(
		default=True,
		description="Reach the IPv4 internet through host NAT. A public IPv4 address needs it. Without it and without a public IPv6 address, the VM reaches only the mesh.",
	)
	is_privileged: bool = Field(
		default=False, description="Whether the guest can reach every tenant through the mesh."
	)
	is_termination_protected: bool = Field(default=False, description="Whether deletion is blocked.")
	sleep_after_idle_seconds: int = Field(
		default=0, ge=0, le=9_223_372_036, description="Idle time before automatic stop. Zero disables it."
	)
	disk_throughput_mibps: int = Field(
		default=0, ge=0, description="Disk throughput limit in MiB/s. Zero removes the limit."
	)
	disk_iops: int = Field(default=0, ge=0, description="Disk IOPS limit. Zero removes the limit.")
	private_network_throughput_mibps: int = Field(
		default=0, ge=0, description="Private network throughput limit in MiB/s. Zero removes the limit."
	)
	public_network_throughput_mibps: int = Field(
		default=0, ge=0, description="Public network throughput limit in MiB/s. Zero removes the limit."
	)
	firewall: FirewallPayload = Field(
		default_factory=FirewallPayload, description="Desired firewall configuration."
	)
	tags: TagMap = Field(default_factory=dict)
	placement_rules: list[AffinityNodePayload] = Field(
		default_factory=list,
		description="Rules that limit the Metal Servers for the virtual machine. Every listed rule or group must hold. Placement uses only the hosts that meet them.",
	)

	@model_validator(mode="after")
	def validate_ipv4_internet_access(self) -> CreateVirtualMachinePayload:
		if self.public_ipv4 and not self.ipv4_internet_access:
			raise ValueError("A public IPv4 address needs ipv4_internet_access.")
		return self

	@model_validator(mode="after")
	def validate_placement_rules(self) -> CreateVirtualMachinePayload:
		"""Apply the shared affinity rule limits."""
		AffinityRules.from_value([node.model_dump() for node in self.placement_rules])
		return self

	def to_domain_request(self, tenant_id: int, image_name: str) -> VirtualMachineCreateRequest:
		"""Build the domain request for this API payload."""
		return VirtualMachineCreateRequest(
			virtual_machine_image=image_name,
			cpu_millicores=self.cpu_millicores,
			memory_mib=self.memory_mib,
			disk_mib=self.disk_mib,
			tenant_id=tenant_id,
			hostname=self.hostname,
			ssh_keys=tuple(self.ssh_keys),
			user_data=self.user_data,
			metadata=self.metadata,
			routes=DEFAULT_ROUTES if self.ipv4_internet_access else (),
			is_privileged=self.is_privileged,
			is_termination_protected=self.is_termination_protected,
			sleep_after_idle_seconds=self.sleep_after_idle_seconds,
			disk_throughput_mibps=self.disk_throughput_mibps,
			disk_iops=self.disk_iops,
			private_network_throughput_mibps=self.private_network_throughput_mibps,
			public_network_throughput_mibps=self.public_network_throughput_mibps,
			firewall=FirewallConfiguration.from_value(self.firewall.model_dump()),
			public_ipv4=self.public_ipv4,
			public_ipv6=self.public_ipv6,
			tags=self.tags,
			placement_rules=AffinityRules.from_value([node.model_dump() for node in self.placement_rules]),
		)


class ResizePayload(PatchPayload):
	"""VM resource and idle shutdown changes."""

	cpu_millicores: int | None = Field(
		default=None,
		ge=MINIMUM_CPU_MILLICORES,
		le=MAXIMUM_CPU_MILLICORES,
		description="New CPU capacity in millicores.",
	)
	memory_mib: int | None = Field(default=None, gt=0, description="New memory capacity in MiB.")
	disk_mib: int | None = Field(default=None, gt=0, description="New root disk capacity in MiB.")
	sleep_after_idle_seconds: int | None = Field(
		default=None,
		ge=0,
		le=9_223_372_036,
		description="New idle time before automatic stop. Zero disables it.",
	)


class DiskUpdatePayload(PatchPayload):
	"""New disk size and disk rate limits."""

	disk_mib: int | None = Field(default=None, gt=0, description="New root disk capacity in MiB.")
	disk_throughput_mibps: int | None = Field(
		default=None, ge=0, description="New disk throughput limit in MiB/s. Zero removes the limit."
	)
	disk_iops: int | None = Field(
		default=None, ge=0, description="New disk IOPS limit. Zero removes the limit."
	)

	def to_domain_changes(self) -> dict[str, int]:
		"""Return the field names that the VM service accepts."""
		changes = self.model_dump(exclude_none=True)
		if "disk_mib" in changes:
			changes["size_mib"] = changes.pop("disk_mib")
		return changes


class NetworkUpdatePayload(PatchPayload):
	"""New IPv4 internet access, network rate limits, or firewall fields."""

	ipv4_internet_access: bool = Field(
		default_factory=lambda: True,
		description="Reach the IPv4 internet through host NAT. A public IPv4 address needs it.",
	)
	private_network_throughput_mibps: int | None = Field(
		default=None,
		ge=0,
		description="New private network throughput limit in MiB/s. Zero removes the limit.",
	)
	public_network_throughput_mibps: int | None = Field(
		default=None,
		ge=0,
		description="New public network throughput limit in MiB/s. Zero removes the limit.",
	)
	wireguard_gateway_access: bool = Field(
		default=False,
		description="Let customer devices on the tenant's WireGuard gateways reach the VM. A network gateway cannot use it.",
	)
	firewall: FirewallUpdatePayload | None = Field(default=None, description="Firewall fields to replace.")


class SSHKeysReplacementPayload(StrictModel):
	"""The complete authorized key list."""

	ssh_keys: list[str] = Field(description="Complete authorized SSH public key list.")


class MetadataReplacementPayload(StrictModel):
	"""The complete custom metadata map."""

	metadata: dict[str, str] = Field(description="Complete custom guest metadata map.")


class PublicIPAssignmentPayload(StrictModel):
	"""The public IP to attach."""

	public_ip: str = Field(
		min_length=1,
		description=f"A reserved direct public IP, or {AUTO_IP_ALLOCATION} for automatic selection.",
	)


class MemorySnapshotConfigurationPayload(StrictModel):
	"""The virtual machine shape that a warm artifact serves."""

	virtual_cpu_count: int | None = Field(
		default=None, ge=1, description="Virtual CPU count served by the warm artifact."
	)
	memory_mib: int | None = Field(
		default=None, ge=1, description="Memory capacity served by the warm artifact in MiB."
	)
	disk_mib: int | None = Field(
		default=None, ge=1, description="Disk capacity served by the warm artifact in MiB."
	)


class SnapshotPayload(StrictModel):
	"""Values that create one image from a virtual machine."""

	title: str = Field(min_length=1, description="Display title for the new image.")
	image_type: ImageType = Field(default="machine", description="Image visibility and intended use.")
	cache_image: bool = Field(default=False, description="Whether hosts may keep this image cached.")
	memory_snapshot: bool = Field(default=False, description="Whether to include guest memory.")
	memory_snapshot_configuration: MemorySnapshotConfigurationPayload = Field(
		default_factory=MemorySnapshotConfigurationPayload,
		description="Shape served by the warm artifact.",
	)
	is_termination_protected: bool = Field(
		default=False, description="Whether deletion of the new image is blocked."
	)
	tags: TagMap = Field(default_factory=dict)


class TerminationProtectionPayload(StrictModel):
	"""The termination protection state to store."""

	enabled: bool = Field(description="New termination protection state.")


class ConsoleTokenPayload(StrictModel):
	"""The console mode that the token opens."""

	mode: Literal["tty", "ssh"] = Field(default="tty", description="Console protocol opened by the token.")


def get_current_state(virtual_machine: VirtualMachine, reported_state: str | None) -> str:
	"""Return the state a caller sees. An Atlas transition hides the host state."""
	if virtual_machine.is_draft:
		return "pending"
	if virtual_machine.is_terminating:
		return "terminating"
	if virtual_machine.active_migration:
		return "migrating"
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
					"architecture": "amd64",
					"cpu_millicores": 2000,
					"memory_mib": 2048,
					"disk_mib": 20480,
					"sleep_after_idle_seconds": 0,
					"is_termination_protected": False,
					"public_ipv4": None,
					"public_ipv6": None,
					"tags": {"env": "prod"},
					"created_at": 1788834165,
				}
			]
		}
	)

	id: str = Field(description="Virtual machine ID.")
	tenant_id: int = Field(description="Tenant that owns the virtual machine.")
	image_id: str = Field(description="Image used to create the virtual machine.")
	architecture: Architecture = Field(description="CPU architecture.")
	cpu_millicores: int = Field(description="CPU capacity in millicores.")
	memory_mib: int = Field(description="Memory capacity in MiB.")
	disk_mib: int = Field(description="Root disk capacity in MiB.")
	sleep_after_idle_seconds: int = Field(description="Idle time before automatic stop. Zero disables it.")
	is_termination_protected: bool = Field(description="Whether deletion is blocked.")
	public_ipv4: PublicIPResponse | None = Field(description="Attached public IPv4 allocation, or null.")
	public_ipv6: PublicIPResponse | None = Field(description="Attached public IPv6 allocation, or null.")
	tags: dict[str, str] = Field(description="Resource tags as key-value pairs.")
	created_at: int = Field(ge=0, description="Creation time as Unix seconds.")

	@classmethod
	def from_document(
		cls, virtual_machine: VirtualMachine, tags: dict[str, str] | None = None
	) -> VirtualMachineResponse:
		"""Build a response from a VM document, or from a query row with its tags."""
		allocations = public_ip_allocations_for(virtual_machine.name)
		return cls(
			id=virtual_machine.name,
			tenant_id=virtual_machine.tenant_id,
			image_id=virtual_machine.virtual_machine_image,
			architecture=virtual_machine.architecture,
			cpu_millicores=virtual_machine.cpu_millicores,
			memory_mib=virtual_machine.memory_mib,
			disk_mib=virtual_machine.disk_mib,
			sleep_after_idle_seconds=virtual_machine.sleep_after_idle_seconds,
			is_termination_protected=bool(virtual_machine.is_termination_protected),
			public_ipv4=allocations.get(4),
			public_ipv6=allocations.get(6),
			tags=read_tags(virtual_machine) if tags is None else tags,
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

	last_known_state: str = Field(
		description="Last state reported by the host, including Atlas transition states."
	)
	state_synced_at: int | None = Field(
		description="Host state synchronization time as Unix seconds, or null."
	)

	@classmethod
	def from_document_and_state(
		cls, virtual_machine: VirtualMachine, state: Any | None, tags: dict[str, str] | None = None
	) -> VirtualMachineListResponse:
		"""Build a list response from Atlas and the stored host state."""
		stored = VirtualMachineResponse.from_document(virtual_machine, tags)
		return cls(
			**stored.model_dump(),
			last_known_state=get_current_state(virtual_machine, state.status if state else None),
			state_synced_at=to_unix_timestamp(state.synced_at) if state else None,
		)


class VirtualMachineCompute(BaseModel):
	"""The compute shape of one virtual machine."""

	cpu_millicores: int = Field(description="CPU capacity in millicores.")
	memory_mib: int = Field(description="Memory capacity in MiB.")
	sleep_after_idle_seconds: int = Field(description="Idle time before automatic stop. Zero disables it.")


class VirtualMachineDisk(BaseModel):
	"""The disk size and its rate limits."""

	size_mib: int = Field(description="Root disk capacity in MiB.")
	throughput_mibps: int = Field(description="Disk throughput limit in MiB/s. Zero means unlimited.")
	iops: int = Field(description="Disk IOPS limit. Zero means unlimited.")
	used_mib: int | None = Field(description="Observed used disk space in MiB, or null when unavailable.")


class FirewallRuleResponse(BaseModel):
	"""One desired firewall allow rule."""

	protocol: FirewallProtocol = Field(description="Allowed IP protocol.")
	ports: str = Field(description="Allowed ports or ranges. Empty means all ports.")
	cidrs: list[str] = Field(description="Source or destination networks in CIDR notation.")


class FirewallResponse(BaseModel):
	"""The complete desired firewall configuration."""

	enabled: bool = Field(description="Whether the firewall enforces these rules.")
	inbound: list[FirewallRuleResponse] = Field(description="Inbound allow rules.")
	outbound: list[FirewallRuleResponse] = Field(description="Outbound allow rules.")


class VirtualMachineNetwork(BaseModel):
	"""The addresses, internet access, and network limits of one virtual machine."""

	ipv4_internet_access: bool = Field(
		description="Whether the guest reaches the IPv4 internet through host NAT."
	)
	public_ipv4: str | None = Field(description="IPv4 address configured on the guest, or null.")
	public_ipv6: str | None = Field(description="IPv6 prefix assigned to the guest, or null.")
	mesh_ipv6: str | None = Field(description="Private WireGuard mesh IPv6 address, or null.")
	mac: str | None = Field(description="Observed network interface MAC address, or null.")
	private_network_throughput_mibps: int = Field(description="Private network throughput limit in MiB/s.")
	public_network_throughput_mibps: int = Field(description="Public network throughput limit in MiB/s.")
	wireguard_gateway_access: bool = Field(
		description="Whether customer devices on the tenant's WireGuard gateways reach the VM."
	)
	firewall: FirewallResponse = Field(description="Desired firewall configuration.")


class VirtualMachineGuest(BaseModel):
	"""The guest configuration of one virtual machine."""

	hostname: str | None = Field(description="Guest hostname, or null.")
	ssh_keys: list[str] = Field(description="Authorized SSH public keys.")
	metadata: dict[str, str] = Field(description="Custom guest metadata.")


class VirtualMachineDetailResponse(BaseModel):
	"""One virtual machine with its state, addresses, and guest configuration."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "vm-00001",
					"tenant_id": 7,
					"image_id": "8f1c2d3e4b5a6978",
					"architecture": "amd64",
					"tags": {"env": "prod"},
					"created_at": 1788834165,
					"is_privileged": False,
					"public_ipv4": None,
					"public_ipv6": None,
					"desired_state": "running",
					"current_state": "running",
					"error": None,
					"compute": {"cpu_millicores": 2000, "memory_mib": 2048, "sleep_after_idle_seconds": 0},
					"disk": {"size_mib": 20480, "throughput_mibps": 0, "iops": 0, "used_mib": 8123},
					"network": {
						"ipv4_internet_access": True,
						"public_ipv4": "203.0.113.10",
						"public_ipv6": "2001:db8:5::7/128",
						"mesh_ipv6": "fdaa:1::5",
						"mac": "52:54:00:12:34:56",
						"private_network_throughput_mibps": 0,
						"public_network_throughput_mibps": 0,
						"wireguard_gateway_access": False,
						"firewall": {"enabled": False, "inbound": [], "outbound": []},
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

	id: str = Field(description="Virtual machine ID.")
	tenant_id: int = Field(description="Tenant that owns the virtual machine.")
	image_id: str = Field(description="Image used to create the virtual machine.")
	architecture: Architecture = Field(description="CPU architecture.")
	tags: dict[str, str] = Field(description="Resource tags as key-value pairs.")
	created_at: int = Field(ge=0, description="Creation time as Unix seconds.")
	is_privileged: bool = Field(description="Whether the guest can reach every tenant through the mesh.")
	public_ipv4: PublicIPResponse | None = Field(description="Attached public IPv4 allocation, or null.")
	public_ipv6: PublicIPResponse | None = Field(description="Attached public IPv6 allocation, or null.")
	desired_state: str | None = Field(description="State requested from the host, or null when unavailable.")
	current_state: str = Field(description="Current state reported by the host or managed by Atlas.")
	error: str | None = Field(description="Current host-reported error, or null.")
	compute: VirtualMachineCompute = Field(description="Compute configuration.")
	disk: VirtualMachineDisk = Field(description="Disk configuration and usage.")
	network: VirtualMachineNetwork = Field(description="Network configuration and addresses.")
	guest: VirtualMachineGuest = Field(description="Guest configuration.")

	@classmethod
	def from_document_and_metal(
		cls, virtual_machine: VirtualMachine, information: MetalVirtualMachine | None
	) -> VirtualMachineDetailResponse:
		"""Build a detailed response from Atlas storage and the live Metal state."""
		desired = information.desired if information else None
		observed = information.observed if information else None
		allocations = public_ip_allocations_for(virtual_machine.name)
		return cls(
			id=virtual_machine.name,
			tenant_id=virtual_machine.tenant_id,
			image_id=virtual_machine.virtual_machine_image,
			architecture=virtual_machine.architecture,
			tags=read_tags(virtual_machine),
			created_at=to_unix_timestamp(virtual_machine.creation),
			is_privileged=bool(virtual_machine.is_privileged),
			public_ipv4=allocations.get(4),
			public_ipv6=allocations.get(6),
			desired_state=desired.state if desired else None,
			current_state=get_current_state(virtual_machine, observed.state if observed else None),
			error=observed.error.message if observed and observed.error else None,
			compute=VirtualMachineCompute(
				cpu_millicores=virtual_machine.cpu_millicores,
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
				ipv4_internet_access=bool(desired)
				and any(
					route.destination == IPV4_INTERNET_DESTINATION and route.is_via_host
					for route in desired.network.routes
				),
				public_ipv4=desired.network.public_ipv4 or None if desired else None,
				public_ipv6=virtual_machine.public_ipv6 or None,
				mesh_ipv6=desired.network.wireguard_mesh_ipv6 or None if desired else None,
				mac=observed.network.mac or None if observed else None,
				private_network_throughput_mibps=(
					desired.network.private_network_throughput_mibps if desired else 0
				),
				public_network_throughput_mibps=(
					desired.network.public_network_throughput_mibps if desired else 0
				),
				wireguard_gateway_access=bool(desired)
				and desired.network.is_accessible_via_wireguard_gateway,
				firewall=FirewallResponse.model_validate(
					desired.network.firewall.as_dict()
					if desired
					else {
						"enabled": False,
						"inbound": [],
						"outbound": [],
					}
				),
			),
			guest=VirtualMachineGuest(
				hostname=desired.guest.hostname or None if desired else None,
				ssh_keys=list(desired.guest.ssh_keys) if desired else [],
				metadata=dict(desired.guest.metadata) if desired else {},
			),
		)


def public_ip_allocations_for(virtual_machine: str) -> dict[int, PublicIPResponse]:
	"""Return each public IP version that belongs to one virtual machine."""
	rows = frappe.get_all(
		"Public IP Allocation",
		filters={"virtual_machine": virtual_machine},
		fields=[
			"name",
			"tenant_id",
			"prefix",
			"pool",
			"version",
			"status",
			"is_reserved",
			"virtual_machine",
			"creation",
		],
	)
	tags = read_tags_for("Public IP Allocation", [row.name for row in rows])
	return {int(row.version): PublicIPResponse.from_document(row, tags[row.name]) for row in rows}


class VirtualMachineComputeUsage(BaseModel):
	"""Cumulative CPU time and current memory use."""

	cpu_microseconds: int = Field(description="Cumulative CPU time since the guest's process started.")
	memory_bytes: int = Field(description="Current memory charged to the Firecracker cgroup, in bytes.")


class VirtualMachineDiskUsage(BaseModel):
	"""The disk's size, configured limits, and sampled I/O rates."""

	size_mib: int = Field(description="Requested disk size.")
	used_mib: int = Field(description="Disk use as of the last reconcile pass.")
	throughput_limit_mibps: int = Field(
		default=0, description="Configured disk throughput limit. Zero means unlimited."
	)
	iops_limit: int = Field(default=0, description="Configured disk IOPS limit. Zero means unlimited.")
	read_bytes_per_second: int = Field(
		default=0, description="Average disk read throughput during the sample interval."
	)
	write_bytes_per_second: int = Field(
		default=0, description="Average disk write throughput during the sample interval."
	)
	read_milli_iops: int = Field(
		default=0, description="Average disk read operations per second, in thousandths of an IOPS."
	)
	write_milli_iops: int = Field(
		default=0, description="Average disk write operations per second, in thousandths of an IOPS."
	)


class VirtualMachineNetworkUsage(BaseModel):
	"""Cumulative unicast IP traffic for the lifetime of the traffic attachment.

	Counters survive guest stops while the attachment remains. Recreating the
	attachment or restarting Metal resets the counters.
	"""

	received_bytes: int = Field(description="Cumulative bytes received by the guest.")
	received_packets: int = Field(description="Cumulative packets received by the guest.")
	sent_bytes: int = Field(description="Cumulative bytes sent by the guest.")
	sent_packets: int = Field(description="Cumulative packets sent by the guest.")
	sent_icmp_packets: int = Field(default=0, description="Cumulative ICMP packets sent by the guest.")
	sent_udp_packets: int = Field(default=0, description="Cumulative UDP packets sent by the guest.")
	sent_tcp_syn_packets: int = Field(default=0, description="Cumulative TCP SYN packets sent by the guest.")
	sent_tcp_rst_packets: int = Field(default=0, description="Cumulative TCP RST packets sent by the guest.")


class VirtualMachineMetricsQuery(StrictModel):
	start: AwareDatetime | None = None
	end: AwareDatetime | None = None
	max_samples: int | None = Field(
		default=None, ge=10, le=2000, description="Maximum samples to return. Metal uses 300 by default."
	)

	@model_validator(mode="after")
	def validate_range(self) -> VirtualMachineMetricsQuery:
		if self.start is not None and self.end is not None and self.start >= self.end:
			raise ValueError("start must precede end")
		return self


class VirtualMachineMetricsSample(BaseModel):
	timestamp: int = Field(description="UTC Unix timestamp in seconds.")
	up: bool
	compute: VirtualMachineComputeUsage
	disk: VirtualMachineDiskUsage
	network: VirtualMachineNetworkUsage


class VirtualMachineMetricsResponse(BaseModel):
	id: str
	samples: list[VirtualMachineMetricsSample]
	sample_interval_seconds: int = Field(default=0, description="Seconds covered by each sample.")

	@classmethod
	def from_metrics(
		cls, virtual_machine_id: str, metrics: MetalVirtualMachineMetrics | None
	) -> VirtualMachineMetricsResponse:
		return cls(
			id=virtual_machine_id,
			sample_interval_seconds=metrics.sample_interval_seconds if metrics is not None else 0,
			samples=[VirtualMachineMetricsSample.model_validate(asdict(sample)) for sample in metrics.samples]
			if metrics is not None
			else [],
		)


class ConsoleTokenResponse(BaseModel):
	"""A single-use console token."""

	model_config = ConfigDict(
		json_schema_extra={"examples": [{"token": "console-token", "mode": "tty", "expires_in": 60}]}
	)

	token: str = Field(description="Single-use console token.")
	mode: Literal["tty", "ssh"] = Field(description="Console protocol opened by the token.")
	expires_in: int = Field(ge=0, description="Seconds until the token expires.")


class HostResponse(BaseModel):
	"""One Metal host that people can be granted SSH access to."""

	model_config = ConfigDict(
		json_schema_extra={
			"examples": [
				{
					"id": "01a0f3e4-a305-77e1-9e93-18b07224f795",
					"title": "metal-osa-2-1",
					"status": "running",
					"tags": {"rack": "r1"},
				}
			]
		}
	)

	id: str = Field(description="Metal Server ID. Use it in the access routes.")
	title: str = Field(description="Host name. People type it in `ssh <email>:<title>@warpgate.<domain>`.")
	status: str = Field(description="Host lifecycle state.")
	tags: dict[str, str] = Field(description="Host tags as key-value pairs.")


class HostAccessGrantPayload(StrictModel):
	"""Open one host, or every host, to one person until a time."""

	email: str = Field(description="Email that the person signs in to Central with.")
	expires_at: datetime = Field(
		description="End of the access, with a time zone. At most 24 hours away by default."
	)


class HostAccessRevokePayload(StrictModel):
	"""Close one host, or every host, to one person now."""

	email: str = Field(description="Email that the person signs in to Central with.")


class WarpgateSessionsClosePayload(StrictModel):
	"""End every live Warpgate session of one person now."""

	email: str = Field(description="Email that the person signs in to Central with.")


class HostAccessResponse(BaseModel):
	"""One active access grant."""

	host_id: str = Field(description="Metal Server ID, or `all`.")
	email: str = Field(description="Email of the person, in lowercase.")
	expires_at: datetime = Field(description="End of the access.")
