from __future__ import annotations

import ipaddress
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic, sleep
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import frappe

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer

PollResult = TypeVar("PollResult")


@dataclass(frozen=True, slots=True)
class ServerSizeData:
	"""Store one server size from a provider."""

	name: str
	architecture: str
	cpu_count: int
	memory_mib: int
	disk_gib: int
	hourly_pricing_usd_cents: int | None
	monthly_pricing_usd_cents: int | None
	provider_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ServerImageData:
	"""Store one server image from a provider."""

	name: str
	os: str
	version: str
	provider_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ServerCreateRequest:
	"""Store the data for one idempotent provider server request."""

	name: str
	server_size: str
	server_image: str
	size_provider_metadata: Mapping[str, Any]
	image_provider_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderServer:
	"""Store the provider state that belongs in an Atlas Server."""

	provider_server_id: str
	status: str | None
	public_ipv4_address: str | None
	provider_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReservedIPAddress:
	"""Store one public IP address from a provider."""

	address: str
	provider_resource_id: str


class ServerPowerAction(StrEnum):
	"""List the provider power operations that Atlas can request."""

	REBOOT = "reboot"
	START = "start"
	STOP = "stop"


class ProviderOperationError(Exception):
	"""Report a server provider operation failure."""

	def __init__(self, message: str, *, code: str = "provider_error", is_retryable: bool = False) -> None:
		super().__init__(message)
		self.code = code
		self.is_retryable = is_retryable


class UnsupportedProviderOperation(ProviderOperationError):
	"""Report an optional operation that a provider does not support."""

	def __init__(self, operation: str) -> None:
		super().__init__(
			f"The server provider does not support {operation}",
			code="unsupported_provider_operation",
		)


ACCEPTED_OS_VERSIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
	{
		"Ubuntu": ("22.04", "24.04", "26.04"),
		"Debian": ("11", "12", "13"),
	}
)


class ServerProvider(ABC):
	"""Define the server provider operations that Atlas uses."""

	provider_type: ClassVar[str]
	credential_fields: ClassVar[tuple[str, ...]]
	ssh_users: ClassVar[tuple[str, ...]] = ("root", "ubuntu")
	error_class: ClassVar[type[ProviderOperationError]] = ProviderOperationError
	setup_poll_interval_seconds: ClassVar[int] = 5
	setup_poll_timeout_seconds: ClassVar[int] = 7_200
	private_address_attempts: ClassVar[int] = 60

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		self.settings: AtlasSettings = settings or frappe.get_single("Atlas Settings")

	@abstractmethod
	def setup_infrastructure(self) -> None:
		"""Set up the named provider resources that Atlas needs."""
		...

	@abstractmethod
	def validate_settings(self) -> None:
		"""Check the provider settings."""
		...

	def validate_server(self, server: "MetalServer") -> None:
		"""Check the Metal Server values that this provider needs."""
		return None

	@abstractmethod
	def validate_credentials(self) -> bool:
		"""Check that the provider credentials permit an API request."""
		...

	@abstractmethod
	def fetch_server_sizes(self) -> tuple[ServerSizeData, ...]:
		"""Return the server sizes from the provider."""
		...

	@abstractmethod
	def fetch_server_images(self) -> tuple[ServerImageData, ...]:
		"""Return the server images from the provider."""
		...

	@abstractmethod
	def ensure_server(self, request: ServerCreateRequest) -> ProviderServer:
		"""Return the server for the identity key, creating it when absent."""
		...

	@abstractmethod
	def prepare_server(self, server: "MetalServer") -> None:
		"""Prepare provider resources before Atlas connects with Secure Shell."""
		...

	@abstractmethod
	def configure_server_network(self, server: "MetalServer") -> None:
		"""Configure the provider network after Secure Shell access is ready."""
		...

	def metald_listen_address(self, server: "MetalServer") -> str:
		"""Return the configured metald address."""
		address = (
			server.public_ipv4_address
			if self.settings.use_public_ip_for_metald
			else server.private_ipv4_address
		)
		if not address:
			raise self.error_class(f"Atlas server {server.name} has no address for metald")
		return address

	@abstractmethod
	def storage_pool_device(self, server: "MetalServer") -> str:
		"""Return the raw block device for the virtual machine storage pool."""
		...

	@abstractmethod
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to a provider server."""
		...

	@abstractmethod
	def delete_server(self, provider_server_id: str, provider_metadata: Mapping[str, Any]) -> None:
		"""Delete a provider server and its owned resources if they exist."""
		...

	def reserve_public_ipv4_address(self) -> ReservedIPAddress:
		"""Reserve one public IPv4 address."""
		raise UnsupportedProviderOperation("public IPv4 address reservation")

	def delete_public_ipv4_address(self, provider_resource_id: str) -> None:
		"""Delete one public IPv4 address."""
		raise UnsupportedProviderOperation("public IPv4 address deletion")

	def attach_public_ipv4_address(
		self, provider_resource_id: str, public_address: str, server: "MetalServer"
	) -> str:
		"""Attach one public IPv4 address and return its host address."""
		raise UnsupportedProviderOperation("public IPv4 address attachment")

	def detach_public_ipv4_address(
		self, provider_resource_id: str, host_address: str | None, server: "MetalServer"
	) -> None:
		"""Detach one public IPv4 address from its provider server."""
		raise UnsupportedProviderOperation("public IPv4 address detachment")

	def promote_ssh_user(self, server: "MetalServer", user: str) -> None:
		"""Promote a provider Secure Shell user to root access."""
		raise ProviderOperationError(f"The provider cannot promote Secure Shell user {user}")

	def wait_for_private_address(self, server: "MetalServer") -> None:
		"""Wait until the configured private address is available."""
		from atlas.atlas.core.ssh import SSHRunner

		device = server.private_network_interface
		expected_address = server.private_ipv4_address
		if not device or not expected_address:
			raise self.error_class("Atlas server has no private network interface or address")

		def has_private_address() -> bool | None:
			"""Report whether the server has its private network address."""
			try:
				result = SSHRunner(server.public_ipv4_address).run_command(
					f"ip -4 -o addr show dev {device} scope global", timeout_seconds=15
				)
			except OSError, subprocess.TimeoutExpired:
				return None
			return True if result.exit_code == 0 and expected_address in result.output else None

		try:
			self.poll(
				has_private_address,
				timeout_seconds=self.private_address_attempts * self.setup_poll_interval_seconds,
				poll_interval_seconds=self.setup_poll_interval_seconds,
				description=f"the private address {expected_address} on {device}",
			)
		except ProviderOperationError as error:
			raise self.error_class(str(error), is_retryable=True) from error

	def run_setup_script(
		self,
		server: "MetalServer",
		script: str,
		*,
		ssh_user: str = "root",
		environment: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
	) -> None:
		"""Run one packaged setup script through Secure Shell."""
		from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask

		task = SSHTask.create_for_script_file(
			target_type=server.doctype,
			target=server.name,
			script_path=script,
			ssh_user=ssh_user,
			environment=environment,
			timeout_seconds=timeout_seconds,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			raise self.error_class(f"Setup script {script} failed", is_retryable=True)

	def poll(
		self,
		operation: Callable[[], PollResult | None],
		*,
		timeout_seconds: int,
		poll_interval_seconds: int,
		description: str,
	) -> PollResult:
		"""Poll one operation until it returns a result or reaches its time limit."""
		deadline = monotonic() + timeout_seconds
		while monotonic() < deadline:
			result = operation()
			if result is not None:
				return result
			sleep(poll_interval_seconds)
		raise self.error_class(f"Timed out while waiting for {description}", is_retryable=True)

	@property
	def private_network_prefix_length(self) -> int:
		"""Return the prefix length of the Atlas private network."""
		return ipaddress.ip_network(self.settings.private_network_cidr, strict=False).prefixlen

	def apply_provider_server(self, server: "MetalServer", provider_server: ProviderServer) -> None:
		"""Apply provider-owned values to an Atlas Server."""
		server.provider_server_id = provider_server.provider_server_id
		if provider_server.status:
			server.status = provider_server.status
		server.public_ipv4_address = provider_server.public_ipv4_address
		self.update_provider_metadata(server, **provider_server.provider_metadata)

	@staticmethod
	def update_provider_metadata(document: "MetalServer", **updates: object) -> None:
		"""Merge provider values into the Server metadata."""
		metadata = frappe.parse_json(document.provider_metadata or "{}")
		if not isinstance(metadata, dict):
			metadata = {}
		metadata.update(updates)
		document.provider_metadata = frappe.as_json(metadata)
