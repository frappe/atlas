from __future__ import annotations

from atlas.atlas.core.server_providers.base import ReservedIPAddress
from atlas.atlas.core.server_providers.scaleway.client import ScalewayClient, ScalewayError
from atlas.atlas.core.server_providers.scaleway.configuration import ScalewayConfiguration


class ScalewayIPAddresses:
	"""Own Scaleway Flexible IP address operations."""

	def __init__(self, client: ScalewayClient, configuration: ScalewayConfiguration) -> None:
		self.client = client
		self.configuration = configuration

	def reserve(self) -> ReservedIPAddress:
		"""Reserve one public IPv4 address."""
		address_data = self.client.request(
			"POST",
			f"/flexible-ip/v1alpha1/zones/{self.configuration.zone}/fips",
			json={"project_id": self.configuration.project_id, "is_ipv6": False},
		)
		address = address_data.get("ip_address") or address_data.get("address")
		provider_resource_id = address_data.get("id")
		if not isinstance(address, str) or not isinstance(provider_resource_id, str):
			raise ScalewayError("Scaleway did not return a Flexible IP address and ID")

		return ReservedIPAddress(address=address, provider_resource_id=provider_resource_id)

	def delete(self, provider_resource_id: str) -> None:
		"""Delete one public IPv4 address if it exists."""
		self.client.request(
			"DELETE",
			f"/flexible-ip/v1alpha1/zones/{self.configuration.zone}/fips/{provider_resource_id}",
			allow_missing=True,
		)

	def attach(self, provider_resource_id: str, provider_server_id: str) -> None:
		"""Attach one public IPv4 address to a provider server."""
		self.client.request(
			"POST",
			f"/flexible-ip/v1alpha1/zones/{self.configuration.zone}/fips/attach",
			json={"fips_ids": [provider_resource_id], "server_id": provider_server_id},
		)

	def detach(self, provider_resource_id: str) -> None:
		"""Detach one public IPv4 address from its provider server."""
		self.client.request(
			"POST",
			f"/flexible-ip/v1alpha1/zones/{self.configuration.zone}/fips/detach",
			json={"fips_ids": [provider_resource_id]},
		)
