from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, override

import requests

from atlas.atlas.core.metal_providers import register
from atlas.atlas.core.metal_providers.base import MetalProvider

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class ScalewayError(Exception):
	"""Raised when Scaleway rejects a provider request."""


@register
class ScalewayProvider(MetalProvider):
	provider_type = "Scaleway"
	base_url = "https://api.scaleway.com"
	private_network_min_prefix = 20
	private_network_max_prefix = 29
	credential_fields = ("scaleway_secret_key", "scaleway_access_key")

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
		self.project_id = self.settings.scaleway_project_id
		self.region = self.settings.scaleway_zone.rsplit("-", 1)[0]
		self.resource_name_prefix = self.settings.resource_name_prefix
		self.organization_id = self.settings.scaleway_organization_id
		self.secret_key = self.settings.get_password("scaleway_secret_key")

	@override
	def validate_settings(self) -> None:
		"""Check the private network CIDR."""
		try:
			network = ipaddress.ip_network(self.settings.private_network_cidr, strict=False)
		except ValueError as error:
			raise ScalewayError(
				f"Invalid Atlas private network CIDR: {self.settings.private_network_cidr}"
			) from error

		if not (self.private_network_min_prefix <= network.prefixlen <= self.private_network_max_prefix):
			raise ScalewayError(
				f"Private network CIDR {self.settings.private_network_cidr} must have a prefix length "
				f"between /{self.private_network_min_prefix} and /{self.private_network_max_prefix}."
			)

	@override
	def bootstrap(self) -> None:
		"""Set up the Scaleway resources used by Atlas."""
		self.settings.scaleway_vpc_id = self.create_vpc()
		self.settings.db_set("scaleway_vpc_id", self.settings.scaleway_vpc_id, update_modified=False)

		self.settings.scaleway_private_network_id = self.create_private_network(
			self.settings.private_network_cidr
		)
		self.settings.db_set(
			"scaleway_private_network_id", self.settings.scaleway_private_network_id, update_modified=False
		)

		self.settings.scaleway_ssh_key_id = self.create_ssh_key(self.settings.public_ssh_key)
		self.settings.db_set("scaleway_ssh_key_id", self.settings.scaleway_ssh_key_id, update_modified=False)

		self.settings.is_metal_provider_setup_completed = 1
		self.settings.save()

	@override
	def validate_credentials(self) -> bool:
		"""Return true when the configured key can access the organization."""
		self._request(
			"GET",
			"/account/v3/projects",
			params={"organization_id": self.organization_id} if self.organization_id else None,
		)
		return True

	@override
	def create_vpc(self) -> str:
		"""Return the existing Atlas VPC ID, or create one."""
		if self.settings.scaleway_vpc_id:
			self._request("GET", f"/vpc/v2/regions/{self.region}/vpcs/{self.settings.scaleway_vpc_id}")
			return self.settings.scaleway_vpc_id

		result = self._request(
			"POST",
			f"/vpc/v2/regions/{self.region}/vpcs",
			json={
				"name": f"{self.resource_name_prefix}vpc",
				"project_id": self.project_id,
			},
		)
		return result["id"]

	@override
	def create_private_network(self, cidr: str) -> str:
		"""Return the existing Atlas private network ID, or create one."""
		if self.settings.scaleway_private_network_id:
			network = self._request(
				"GET",
				f"/vpc/v2/regions/{self.region}/private-networks/{self.settings.scaleway_private_network_id}",
			)
			self._validate_private_network(network, cidr)
			return self.settings.scaleway_private_network_id

		result = self._request(
			"POST",
			f"/vpc/v2/regions/{self.region}/private-networks",
			json={
				"name": f"{self.resource_name_prefix}private-network",
				"project_id": self.project_id,
				"subnets": [cidr],
				"vpc_id": self.settings.scaleway_vpc_id,
			},
		)
		return result["id"]

	def _validate_private_network(self, network: dict, cidr: str) -> None:
		if network.get("vpc_id") != self.settings.scaleway_vpc_id:
			raise ScalewayError(
				f"Private network {self.settings.scaleway_private_network_id} does not belong to VPC "
				f"{self.settings.scaleway_vpc_id}"
			)
		try:
			expected_network = ipaddress.ip_network(cidr, strict=False)
		except ValueError as error:
			raise ScalewayError(f"Invalid Atlas private network CIDR: {cidr}") from error

		for subnet in network.get("subnets", []):
			try:
				actual_network = ipaddress.ip_network(subnet.get("subnet", subnet), strict=False)
			except ValueError:
				continue
			if actual_network == expected_network:
				return

		raise ScalewayError(
			f"Private network {self.settings.scaleway_private_network_id} does not contain CIDR {cidr}"
		)

	@override
	def create_ssh_key(self, public_key: str) -> str:
		"""Return the existing Atlas SSH key ID, or create one."""
		configured_key_id = self.settings.scaleway_ssh_key_id
		if configured_key_id:
			key = self._request("GET", f"/iam/v1alpha1/ssh-keys/{configured_key_id}")
			if self._key_identity(key.get("public_key")) != self._key_identity(public_key):
				raise ScalewayError(
					f"SSH key {configured_key_id} does not match Atlas Settings.public_ssh_key"
				)
			return configured_key_id

		key_identity = self._key_identity(public_key)
		for key in self._list_ssh_keys():
			if key_identity and self._key_identity(key.get("public_key")) == key_identity:
				return str(key["id"])

		result = self._request(
			"POST",
			"/iam/v1alpha1/ssh-keys",
			json={
				"name": f"{self.resource_name_prefix}ssh-key",
				"public_key": public_key,
				"project_id": self.project_id,
			},
		)
		return str(result["id"])

	def _list_ssh_keys(self) -> list[dict]:
		result = self._request("GET", "/iam/v1alpha1/ssh-keys", params={"project_id": self.project_id})
		return result.get("ssh_keys", [])

	@staticmethod
	def _key_identity(public_key: str | None) -> str:
		return " ".join((public_key or "").split()[:2])

	def _request(self, method: str, path: str, **kwargs: object) -> dict:
		response = requests.request(
			method,
			f"{self.base_url}{path}",
			headers={"X-Auth-Token": self.secret_key},
			timeout=30,
			**kwargs,
		)
		if response.status_code >= 400:
			raise ScalewayError(f"{method} {path} failed with status {response.status_code}: {response.text}")
		return response.json() if response.content else {}
