from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from .client import ScalewayError

if TYPE_CHECKING:
	from .provider import ScalewayProvider


class ScalewayInfrastructure:
	"""Manage the Scaleway resources shared by Atlas servers."""

	def __init__(self, provider: "ScalewayProvider") -> None:
		self.provider = provider

	def validate_settings(self) -> None:
		"""Validate the Scaleway private network settings."""
		self.provider._subscription_period()
		try:
			network = ipaddress.ip_network(self.provider.settings.private_network_cidr, strict=False)
		except ValueError as error:
			raise ScalewayError(
				f"Invalid Atlas private network CIDR: {self.provider.settings.private_network_cidr}"
			) from error

		if not (
			self.provider.private_network_min_prefix
			<= network.prefixlen
			<= self.provider.private_network_max_prefix
		):
			raise ScalewayError(
				f"Private network CIDR {self.provider.settings.private_network_cidr} must have a prefix length "
				f"between /{self.provider.private_network_min_prefix} and /{self.provider.private_network_max_prefix}."
			)

	def bootstrap(self) -> None:
		"""Create and store the Scaleway resources used by Atlas."""
		settings = self.provider.settings
		settings.scaleway_vpc_id = self.create_vpc()
		settings.db_set("scaleway_vpc_id", settings.scaleway_vpc_id, update_modified=False)

		settings.scaleway_private_network_id = self.create_private_network(settings.private_network_cidr)
		settings.db_set(
			"scaleway_private_network_id", settings.scaleway_private_network_id, update_modified=False
		)

		settings.scaleway_ssh_key_id = self.create_ssh_key(settings.public_ssh_key)
		settings.db_set("scaleway_ssh_key_id", settings.scaleway_ssh_key_id, update_modified=False)

		settings.is_server_provider_setup_completed = 1
		settings.save()

	def validate_credentials(self) -> bool:
		"""Return true when the configured key can access Scaleway."""
		self.provider._request(
			"GET",
			"/account/v3/projects",
			params={"organization_id": self.provider.organization_id}
			if self.provider.organization_id
			else None,
		)
		return True

	def create_vpc(self) -> str:
		"""Return the Atlas VPC ID, or create an Atlas VPC."""
		settings = self.provider.settings
		if settings.scaleway_vpc_id:
			self.provider._request(
				"GET", f"/vpc/v2/regions/{self.provider.region}/vpcs/{settings.scaleway_vpc_id}"
			)
			return settings.scaleway_vpc_id

		result = self.provider._request(
			"POST",
			f"/vpc/v2/regions/{self.provider.region}/vpcs",
			json={
				"name": f"{self.provider.resource_name_prefix}vpc",
				"project_id": self.provider.project_id,
			},
		)
		return result["id"]

	def create_private_network(self, cidr: str) -> str:
		"""Return the Atlas private network ID, or create a network."""
		settings = self.provider.settings
		if settings.scaleway_private_network_id:
			network = self.provider._request(
				"GET",
				f"/vpc/v2/regions/{self.provider.region}/private-networks/{settings.scaleway_private_network_id}",
			)
			self._validate_private_network(network, cidr)
			return settings.scaleway_private_network_id

		result = self.provider._request(
			"POST",
			f"/vpc/v2/regions/{self.provider.region}/private-networks",
			json={
				"name": f"{self.provider.resource_name_prefix}private-network",
				"project_id": self.provider.project_id,
				"subnets": [cidr],
				"vpc_id": settings.scaleway_vpc_id,
			},
		)
		return result["id"]

	def create_ssh_key(self, public_key: str) -> str:
		"""Return the Atlas SSH key ID, or create an SSH key."""
		settings = self.provider.settings
		configured_key_id = settings.scaleway_ssh_key_id
		if configured_key_id:
			key = self.provider._request("GET", f"/iam/v1alpha1/ssh-keys/{configured_key_id}")
			if self._key_identity(key.get("public_key")) != self._key_identity(public_key):
				raise ScalewayError(
					f"SSH key {configured_key_id} does not match Atlas Settings.public_ssh_key"
				)
			return configured_key_id

		key_identity = self._key_identity(public_key)
		response = self.provider._request(
			"GET", "/iam/v1alpha1/ssh-keys", params={"project_id": self.provider.project_id}
		)
		for key in response.get("ssh_keys", []):
			if key_identity and self._key_identity(key.get("public_key")) == key_identity:
				return str(key["id"])

		result = self.provider._request(
			"POST",
			"/iam/v1alpha1/ssh-keys",
			json={
				"name": f"{self.provider.resource_name_prefix}ssh-key",
				"public_key": public_key,
				"project_id": self.provider.project_id,
			},
		)
		return str(result["id"])

	def _validate_private_network(self, network: dict, cidr: str) -> None:
		settings = self.provider.settings
		if network.get("vpc_id") != settings.scaleway_vpc_id:
			raise ScalewayError(
				f"Private network {settings.scaleway_private_network_id} does not belong to VPC "
				f"{settings.scaleway_vpc_id}"
			)

		expected_network = ipaddress.ip_network(cidr, strict=False)
		for subnet in network.get("subnets", []):
			try:
				actual_network = ipaddress.ip_network(subnet.get("subnet", subnet), strict=False)
			except ValueError:
				continue
			if actual_network == expected_network:
				return

		raise ScalewayError(
			f"Private network {settings.scaleway_private_network_id} does not contain CIDR {cidr}"
		)

	@staticmethod
	def _key_identity(public_key: str | None) -> str:
		return " ".join((public_key or "").split()[:2])
