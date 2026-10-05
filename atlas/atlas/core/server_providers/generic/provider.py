from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, override

import frappe

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.base import (
	ProviderOperationError,
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


class GenericError(ProviderOperationError):
	"""Report a Generic provider failure."""


@register
class GenericProvider(ServerProvider):
	"""Use hosts that an operator prepares and registers after a host inspection.

	The host network routes each public address to any host, so Metal only
	adds an attached address to the virtual machine port.
	"""

	provider_type = "Generic"
	credential_fields = ()
	ssh_users = ("root",)
	error_class = GenericError

	@override
	def validate_settings(self) -> None:
		"""Reject automatic host creation."""
		if self.settings.auto_spawn_metal_server:
			raise GenericError("The Generic provider cannot create a Metal Server automatically")

	@override
	def validate_server(self, server: "MetalServer") -> None:
		"""Require the storage pool device that host registration stores."""
		self.get_storage_pool_device(server)

	@override
	def validate_credentials(self) -> bool:
		"""Return true because the provider has no credentials."""
		return True

	@override
	def setup_infrastructure(self) -> None:
		"""Mark the setup complete because the operator owns the infrastructure."""
		self.settings.is_server_provider_setup_completed = 1
		self.settings.save()

	@override
	def fetch_server_sizes(self) -> tuple[ServerSizeData, ...]:
		"""Return no sizes because the provider has no catalog."""
		return ()

	@override
	def fetch_server_images(self) -> tuple[ServerImageData, ...]:
		"""Return no images because the provider has no catalog."""
		return ()

	@override
	def ensure_server(self, request: ServerCreateRequest) -> ProviderServer:
		"""Refuse creation because the operator records each host."""
		raise UnsupportedProviderOperation("server creation")

	@override
	def prepare_server(self, server: "MetalServer") -> None:
		"""Do nothing because the operator prepares the host."""

	@override
	def configure_server_network(self, server: "MetalServer") -> None:
		"""Check the private address that the operator configured, and store its interface MAC."""
		self.wait_for_private_address(server)

	@override
	def storage_pool_device(self, server: "MetalServer") -> str:
		"""Refuse a host without the storage pool device that registration stores."""
		raise GenericError(f"Metal Server {server.name} has no registered storage pool device")

	@override
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply the power action through the host BMC. Refuse it without a BMC driver."""
		if self.settings.bmc_driver != "Redfish":
			raise UnsupportedProviderOperation(f"the {action} power action")

		servers = frappe.get_all(
			"Metal Server",
			filters={"provider_server_id": provider_server_id, "status": ("!=", "Deleted")},
			pluck="name",
		)
		if len(servers) != 1:
			raise GenericError(
				f"{len(servers)} Metal Servers use the provider server ID {provider_server_id}"
			)

		server: "MetalServer" = frappe.get_doc("Metal Server", servers[0])
		if not server.bmc_url:
			raise GenericError(f"Metal Server {server.name} has no BMC details")

		server.bmc_client.set_power_state(action)

	@override
	def delete_server(self, provider_server_id: str, provider_metadata: Mapping[str, object]) -> None:
		"""Do nothing because the operator reclaims the host."""

	@override
	def delete_public_ip_address(self, provider_resource_id: str) -> None:
		"""Do nothing because the operator owns the address."""

	@override
	def attach_public_ip_address(
		self, provider_resource_id: str, public_address: str, server: "MetalServer"
	) -> str:
		"""Return the public address, which the host network already routes."""
		if provider_resource_id != public_address:
			raise GenericError("A Generic public IPv4 address must use itself as the provider resource ID")
		return public_address

	@override
	def detach_public_ip_address(
		self, provider_resource_id: str, host_address: str | None, server: "MetalServer"
	) -> None:
		"""Do nothing because the host network keeps the route."""
