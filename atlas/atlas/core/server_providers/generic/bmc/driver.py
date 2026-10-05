from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import TYPE_CHECKING, override

import frappe

from atlas.atlas.core.server_providers.base import (
	ProviderServer,
	ServerCreateRequest,
	ServerImageData,
	ServerPowerAction,
	ServerPowerStatus,
	ServerSizeData,
	UnsupportedProviderOperation,
)
from atlas.atlas.core.server_providers.generic.bmc.client import RedfishClient, RedfishError, RedfishSystem
from atlas.atlas.core.server_providers.generic.provider import GenericProvider

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class BMCDriver(GenericProvider):
	"""Register Generic hosts and control power through their BMC."""

	error_class = RedfishError
	is_registration_only = True

	@override
	def validate_server(self, server: "MetalServer") -> None:
		"""Require discovery through the registration flow for a new record."""
		if server.is_new() and not isinstance(getattr(server, "_redfish_registration", None), RedfishSystem):
			frappe.throw(frappe._("Use Register on the Metal Server form to register a BMC system."))

	@override
	def import_server(self, server: "MetalServer") -> None:
		"""Discover an existing system and fill its registration values."""
		system = RedfishClient(
			server.redfish_url or "",
			server.redfish_username or "",
			server.get_password("redfish_password", raise_exception=False) or "",
		).discover_system()
		server.redfish_url = system.url
		server._redfish_registration = system
		self.apply_provider_server(
			server,
			ProviderServer(
				provider_server_id="redfish-" + sha256(system.url.encode()).hexdigest()[:32],
				status="Pending",
				public_ipv4_address=None,
				provider_metadata={"id": system.id, "name": system.name, "uuid": system.uuid},
			),
		)

	@override
	def read_power_status(self, provider_server_id: str) -> ServerPowerStatus:
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
		raise UnsupportedProviderOperation("BMC infrastructure setup")

	@override
	def validate_credentials(self) -> bool:
		"""Check access to every active registered Redfish system."""
		provider_ids = frappe.get_all(
			"Metal Server",
			filters={"provider_server_id": ["like", "redfish-%"], "status": ["!=", "Deleted"]},
			pluck="provider_server_id",
			order_by="name",
		)
		if not provider_ids:
			raise RedfishError("Register a BMC Metal Server before validating credentials")

		for provider_id in provider_ids:
			client = self._client(provider_id)
			if client.discover_system().url != client.url:
				raise RedfishError("Redfish returned a different system from the registered URL")
		return True

	@override
	def fetch_server_sizes(self) -> tuple[ServerSizeData, ...]:
		raise UnsupportedProviderOperation("BMC server size discovery")

	@override
	def fetch_server_images(self) -> tuple[ServerImageData, ...]:
		raise UnsupportedProviderOperation("BMC server image discovery")

	@override
	def ensure_server(self, request: ServerCreateRequest) -> ProviderServer:
		raise UnsupportedProviderOperation("BMC server creation")

	@override
	def prepare_server(self, server: "MetalServer") -> None:
		raise UnsupportedProviderOperation("BMC server preparation")

	@override
	def configure_server_network(self, server: "MetalServer") -> None:
		raise UnsupportedProviderOperation("BMC server network configuration")

	@override
	def storage_pool_device(self, server: "MetalServer") -> str:
		raise UnsupportedProviderOperation("BMC storage pool device discovery")

	@override
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		if action == ServerPowerAction.START:
			self._client(provider_server_id).power_on()
		elif action == ServerPowerAction.STOP:
			self._client(provider_server_id).power_off()
		elif action == ServerPowerAction.REBOOT:
			self._client(provider_server_id).reboot()
		else:
			raise UnsupportedProviderOperation(f"the BMC {action} power action")

	@override
	def delete_server(self, provider_server_id: str, provider_metadata: Mapping[str, object]) -> None:
		raise UnsupportedProviderOperation("BMC server deletion")

	@override
	def delete_public_ip_address(self, provider_resource_id: str) -> None:
		raise UnsupportedProviderOperation("public IP address deletion")

	@override
	def attach_public_ip_address(
		self, provider_resource_id: str, public_address: str, server: "MetalServer"
	) -> str:
		raise UnsupportedProviderOperation("public IP address attachment")

	@override
	def detach_public_ip_address(
		self, provider_resource_id: str, host_address: str | None, server: "MetalServer"
	) -> None:
		raise UnsupportedProviderOperation("public IP address detachment")
