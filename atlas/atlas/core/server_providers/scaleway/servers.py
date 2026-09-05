from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import ClassVar

from atlas.atlas.core.server_providers.base import (
	ProviderServer,
	ServerCreateRequest,
	ServerPowerAction,
)
from atlas.atlas.core.server_providers.scaleway.catalog import ScalewayCatalog
from atlas.atlas.core.server_providers.scaleway.client import ScalewayClient, ScalewayError
from atlas.atlas.core.server_providers.scaleway.configuration import ScalewayConfiguration
from atlas.atlas.core.server_providers.scaleway.partitioning import ScalewayPartitioning


class ScalewayServers:
	"""Own Scaleway Elastic Metal server operations."""

	server_status_map: ClassVar[dict[str, str]] = {
		"delivering": "Installing",
		"installing": "Installing",
		"ready": "Installing",
		"running": "Installing",
		"stopped": "Stopped",
		"error": "Failed",
	}

	def __init__(
		self,
		client: ScalewayClient,
		configuration: ScalewayConfiguration,
		catalog: ScalewayCatalog,
		partitioning: ScalewayPartitioning,
	) -> None:
		self.client = client
		self.configuration = configuration
		self.catalog = catalog
		self.partitioning = partitioning

	def ensure(self, request: ServerCreateRequest) -> ProviderServer:
		"""Return the named server, and create it when it does not exist."""
		remote_server = self.find(request.name)
		was_created = remote_server is None
		if remote_server is None:
			remote_server = self.create(request)

		return self.to_provider_server(remote_server, was_created=was_created)

	def create(self, request: ServerCreateRequest) -> Mapping:
		"""Create one Scaleway server from an Atlas request."""
		subscription_period = self.subscription_period
		offer_for_id = self.catalog.offer(request.size_provider_metadata, request.server_size, "hourly")
		offer_id = self.catalog.offer_id(offer_for_id, request.server_size, subscription_period)
		offer_for_options = self.catalog.offer(
			request.size_provider_metadata, request.server_size, subscription_period
		)
		option_id = self.catalog.private_network_option_id(
			offer_for_options, request.server_size, subscription_period
		)
		return self.client.request(
			"POST",
			f"/baremetal/v1/zones/{self.configuration.zone}/servers",
			json={
				"offer_id": offer_id,
				"option_ids": [option_id],
				"project_id": self.configuration.project_id,
				"name": request.name,
				"description": f"Atlas server {request.name}",
				"tags": [self.server_tag(request.name)],
				"install": self.install_configuration(request, offer_id),
			},
		)

	def find(self, server_name: str) -> Mapping | None:
		"""Return the server with the Atlas identity tag."""
		response = self.client.request(
			"GET",
			f"/baremetal/v1/zones/{self.configuration.zone}/servers",
			params={
				"project_id": self.configuration.project_id,
				"tags": [self.server_tag(server_name)],
			},
		)
		servers = response.get("servers", [])
		if not isinstance(servers, list) or not all(isinstance(item, Mapping) for item in servers):
			raise ScalewayError("Scaleway response has invalid servers")
		if len(servers) > 1:
			raise ScalewayError(f"Scaleway returned multiple servers for Atlas server {server_name}")
		return servers[0] if servers else None

	def fetch(self, provider_server_id: str) -> Mapping:
		"""Return one Scaleway server."""
		return self.client.request(
			"GET", f"/baremetal/v1/zones/{self.configuration.zone}/servers/{provider_server_id}"
		)

	def ensure_private_network(self, provider_server_id: str) -> Mapping:
		"""Return the private network attachment, and create it when it does not exist."""
		private_network = self.private_network(provider_server_id)
		if private_network is not None:
			return private_network
		if not self.configuration.private_network_id:
			raise ScalewayError("Atlas Settings has no Scaleway private network ID")

		return self.client.request(
			"POST",
			f"/baremetal/v1/zones/{self.configuration.zone}/servers/{provider_server_id}/private-networks",
			json={"private_network_id": self.configuration.private_network_id},
		)

	def private_network(self, provider_server_id: str) -> Mapping | None:
		"""Return the Atlas private network attachment for one server."""
		response = self.client.request(
			"GET",
			f"/baremetal/v1/zones/{self.configuration.zone}/server-private-networks",
			params={
				"server_id": provider_server_id,
				"private_network_id": self.configuration.private_network_id,
			},
		)
		private_networks = response.get("server_private_networks", [])
		if not isinstance(private_networks, list) or not all(
			isinstance(item, Mapping) for item in private_networks
		):
			raise ScalewayError("Scaleway response has invalid server private networks")
		return next(
			(
				private_network
				for private_network in private_networks
				if private_network.get("private_network_id") == self.configuration.private_network_id
			),
			None,
		)

	def private_ipv4_address(self, private_network_interface_id: str) -> str:
		"""Return the private IPv4 address for one Scaleway network interface."""
		response = self.client.request(
			"GET",
			f"/ipam/v1/regions/{self.configuration.region}/ips",
			params={
				"project_id": self.configuration.project_id,
				"resource_id": private_network_interface_id,
			},
		)
		for address in response.get("ips", []):
			if address.get("is_ipv6") or not isinstance(address.get("address"), str):
				continue
			interface = ipaddress.ip_interface(address["address"])
			if isinstance(interface, ipaddress.IPv4Interface):
				return str(interface.ip)

		raise ScalewayError(f"Scaleway IPAM has no private IPv4 address for {private_network_interface_id}")

	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to a Scaleway server."""
		self.client.request(
			"POST",
			f"/baremetal/v1/zones/{self.configuration.zone}/servers/{provider_server_id}/{action.value}",
			json={},
		)

	def delete(self, provider_server_id: str) -> None:
		"""Delete one Scaleway server if it exists."""
		self.client.request(
			"DELETE",
			f"/baremetal/v1/zones/{self.configuration.zone}/servers/{provider_server_id}",
			allow_missing=True,
		)

	def install_configuration(self, request: ServerCreateRequest, offer_id: str) -> dict:
		"""Return the operating system installation configuration."""
		operating_system_id = request.image_provider_metadata.get("id")
		if not isinstance(operating_system_id, str):
			raise ScalewayError(f"Server Image {request.server_image} has no Scaleway operating system ID")

		configuration = {
			"os_id": operating_system_id,
			"hostname": request.name,
			"ssh_key_ids": [self.configuration.ssh_key_id],
		}
		default_schema = self.client.request(
			"GET",
			f"/baremetal/v1/zones/{self.configuration.zone}/partitioning-schemas/default",
			allow_missing=True,
			params={"offer_id": offer_id, "os_id": operating_system_id},
		)
		partitioning_schema = self.partitioning.get_schema(default_schema)
		if partitioning_schema is not None:
			configuration["partitioning_schema"] = partitioning_schema
		return configuration

	@property
	def subscription_period(self) -> str:
		"""Return the configured Scaleway subscription period."""
		if self.configuration.billing_cycle not in {"Hourly", "Monthly"}:
			raise ScalewayError("Scaleway Machine Billing Cycle must be Hourly or Monthly")
		return self.configuration.billing_cycle.lower()

	@classmethod
	def to_provider_server(cls, remote_server: Mapping, *, was_created: bool = False) -> ProviderServer:
		"""Convert one Scaleway response to provider-neutral data."""
		provider_server_id = remote_server.get("id")
		if not isinstance(provider_server_id, str):
			raise ScalewayError("Scaleway did not return a server ID")

		return ProviderServer(
			provider_server_id=provider_server_id,
			status=cls.server_status_map.get(remote_server.get("status")),
			public_ipv4_address=cls.public_ipv4_address(remote_server.get("ips", [])),
			provider_metadata={"server": dict(remote_server)},
			was_created=was_created,
		)

	@staticmethod
	def public_ipv4_address(addresses: object) -> str | None:
		"""Return the public IPv4 address from a Scaleway address list."""
		if not isinstance(addresses, list):
			return None
		for address in addresses:
			if isinstance(address, Mapping) and address.get("version") == "IPv4":
				value = address.get("address")
				return value if isinstance(value, str) else None
		return None

	@staticmethod
	def server_tag(server_name: str) -> str:
		"""Return the stable Atlas identity tag for one provider server."""
		return f"atlas-server:{server_name}"
