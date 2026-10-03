from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, override

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.base import (
	ProviderServer,
	ServerCreateRequest,
	ServerImageData,
	ServerPowerAction,
	ServerProvider,
	ServerSizeData,
	UnsupportedProviderOperation,
)

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


@register
class RedfishProvider(ServerProvider):
	"""Define the Redfish provider without remote operations."""

	provider_type = "Redfish"
	credential_fields = ()

	@override
	def validate_settings(self) -> None:
		"""Accept the provider selection without connection settings."""
		return None

	@override
	def setup_infrastructure(self) -> None:
		raise UnsupportedProviderOperation("Redfish infrastructure setup")

	@override
	def validate_credentials(self) -> bool:
		raise UnsupportedProviderOperation("Redfish credential validation")

	@override
	def fetch_server_sizes(self) -> tuple[ServerSizeData, ...]:
		raise UnsupportedProviderOperation("Redfish server size discovery")

	@override
	def fetch_server_images(self) -> tuple[ServerImageData, ...]:
		raise UnsupportedProviderOperation("Redfish server image discovery")

	@override
	def ensure_server(self, request: ServerCreateRequest) -> ProviderServer:
		raise UnsupportedProviderOperation("Redfish server creation")

	@override
	def prepare_server(self, server: "MetalServer") -> None:
		raise UnsupportedProviderOperation("Redfish server preparation")

	@override
	def configure_server_network(self, server: "MetalServer") -> None:
		raise UnsupportedProviderOperation("Redfish server network configuration")

	@override
	def storage_pool_device(self, server: "MetalServer") -> str:
		raise UnsupportedProviderOperation("Redfish storage pool device discovery")

	@override
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		raise UnsupportedProviderOperation(f"the Redfish {action} power action")

	@override
	def delete_server(self, provider_server_id: str, provider_metadata: Mapping[str, object]) -> None:
		raise UnsupportedProviderOperation("Redfish server deletion")
