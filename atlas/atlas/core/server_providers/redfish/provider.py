from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import TYPE_CHECKING, override

import frappe

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
from atlas.atlas.core.server_providers.redfish.client import RedfishClient, RedfishError, RedfishPowerStatus

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


@register
class RedfishProvider(ServerProvider):
	"""Manage the BMC of a registered Redfish system."""

	provider_type = "Redfish"
	credential_fields = ()
	error_class = RedfishError

	@override
	def validate_settings(self) -> None:
		"""Reject automatic host creation for existing Redfish machines."""
		if self.settings.auto_spawn_metal_server:
			raise RedfishError("The Redfish provider cannot create a Metal Server automatically")

	def read_power_status(self, provider_server_id: str) -> RedfishPowerStatus:
		"""Read the registered system with its saved per-server credentials."""
		return self._client(provider_server_id).read_power_status()

	def _client(self, provider_server_id: str) -> RedfishClient:
		name = frappe.db.get_value(
			"Metal Server", {"provider_server_id": provider_server_id, "status": ["!=", "Deleted"]}
		)
		if not name:
			raise RedfishError("No active Metal Server has this Redfish provider ID")
		server: MetalServer = frappe.get_doc("Metal Server", name)
		if not server.redfish_url:
			raise RedfishError("The Metal Server has no registered Redfish URL")
		client = RedfishClient(
			server.redfish_url,
			server.redfish_username or "",
			server.get_password("redfish_password", raise_exception=False) or "",
		)
		if provider_server_id != "redfish-" + sha256(client.url.encode()).hexdigest()[:32]:
			raise RedfishError("The Redfish URL does not match the registered provider ID")
		return client

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
		if action == ServerPowerAction.START:
			self._client(provider_server_id).power_on()
		else:
			raise UnsupportedProviderOperation(f"the Redfish {action} power action")

	@override
	def delete_server(self, provider_server_id: str, provider_metadata: Mapping[str, object]) -> None:
		raise UnsupportedProviderOperation("Redfish server deletion")
