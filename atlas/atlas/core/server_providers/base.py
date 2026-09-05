from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

import frappe

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.server.doctype.server.server import Server


@dataclass(frozen=True, slots=True)
class ServerSizeData:
	"""Store one server size from a provider."""

	size: str
	cpu_count: int
	memory_mib: int
	disk_gib: int
	hourly_pricing_usd_cents: int | None
	monthly_pricing_usd_cents: int | None
	provider_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ServerImageData:
	"""Store one server image from a provider."""

	image: str
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
	was_created: bool = False


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
		"""Return the named server, and create it when it does not exist."""
		...

	@abstractmethod
	def prepare_server(self, server: "Server") -> None:
		"""Prepare provider resources before Atlas connects with Secure Shell."""
		...

	@abstractmethod
	def configure_server_network(self, server: "Server") -> None:
		"""Configure the provider network after Secure Shell access is ready."""
		...

	@abstractmethod
	def get_storage_pool_device(self, server: "Server") -> str:
		"""Return the raw block device for the virtual machine storage pool."""
		...

	@abstractmethod
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to a provider server."""
		...

	@abstractmethod
	def delete_server(self, provider_server_id: str) -> None:
		"""Delete a provider server if it exists."""
		...

	def reserve_public_ipv4_address(self) -> ReservedIPAddress:
		"""Reserve one public IPv4 address."""
		raise UnsupportedProviderOperation("public IPv4 address reservation")

	def delete_public_ipv4_address(self, provider_resource_id: str) -> None:
		"""Delete one public IPv4 address."""
		raise UnsupportedProviderOperation("public IPv4 address deletion")

	def attach_public_ipv4_address(self, provider_resource_id: str, server: "Server") -> None:
		"""Attach one public IPv4 address to a provider server."""
		raise UnsupportedProviderOperation("public IPv4 address attachment")

	def detach_public_ipv4_address(self, provider_resource_id: str) -> None:
		"""Detach one public IPv4 address from its provider server."""
		raise UnsupportedProviderOperation("public IPv4 address detachment")

	def promote_ssh_user(self, server: "Server", user: str) -> None:
		"""Promote a provider Secure Shell user to root access."""
		raise ProviderOperationError(f"The provider cannot promote Secure Shell user {user}")
