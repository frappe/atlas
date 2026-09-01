from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

import requests

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.base import ACCEPTED_OS_VERSIONS, ImageInfo, ServerProvider, SizeInfo

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class ScalewayError(Exception):
	"""Raised when Scaleway rejects a provider request."""


@register
class ScalewayProvider(ServerProvider):
	provider_type = "Scaleway"
	base_url = "https://api.scaleway.com"
	price_scale = 100
	private_network_min_prefix = 20
	private_network_max_prefix = 29
	credential_fields = ("scaleway_secret_key", "scaleway_access_key")

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
		self.project_id = self.settings.scaleway_project_id
		self.zone = self.settings.scaleway_zone
		self.region = self.zone.rsplit("-", 1)[0]
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
			"scaleway_private_network_id",
			self.settings.scaleway_private_network_id,
			update_modified=False,
		)

		self.settings.scaleway_ssh_key_id = self.create_ssh_key(self.settings.public_ssh_key)
		self.settings.db_set("scaleway_ssh_key_id", self.settings.scaleway_ssh_key_id, update_modified=False)

		self.settings.is_server_provider_setup_completed = 1
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
		response = self._request("GET", "/iam/v1alpha1/ssh-keys", params={"project_id": self.project_id})
		for key in response.get("ssh_keys", []):
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

	@override
	def fetch_server_sizes(self) -> tuple[SizeInfo, ...]:
		"""Return and merge the Elastic Metal offers for the configured zone."""
		response = self._request("GET", f"/baremetal/v1/zones/{self.zone}/offers?page_size=100")
		offers = response.get("offers", [])
		if not isinstance(offers, list) or not all(isinstance(offer, Mapping) for offer in offers):
			raise ScalewayError("Scaleway response has invalid offers")

		sizes: dict[str, SizeInfo] = {}
		for offer in offers:
			sizes[offer["name"]] = self._merge_offer(sizes.get(offer["name"]), offer)
		return tuple(sizes.values())

	@override
	def fetch_server_images(self) -> tuple[ImageInfo, ...]:
		"""Return the Ubuntu and Debian OS images available in the configured zone."""
		response = self._request("GET", f"/baremetal/v1/zones/{self.zone}/os?page_size=100")
		images = response.get("os", [])
		if not isinstance(images, list) or not all(isinstance(image, Mapping) for image in images):
			raise ScalewayError("Scaleway response has invalid OS images")

		return tuple(filter(None, (self._accepted_image(image) for image in images)))

	# Internal methods

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

	@classmethod
	def _merge_offer(cls, size: SizeInfo | None, offer: Mapping) -> SizeInfo:
		metadata = dict(size.provider_metadata) if size else {}
		metadata[offer.get("subscription_period")] = dict(offer)

		hourly_amount = cls._money_to_usd_cents(offer.get("price_per_hour"))
		if hourly_amount is None and size:
			hourly_amount = size.hourly_pricing_usd_cents
		monthly_amount = cls._money_to_usd_cents(offer.get("price_per_month"))
		if monthly_amount is None and size:
			monthly_amount = size.monthly_pricing_usd_cents
		hourly_amount, monthly_amount = cls._fill_missing_prices(hourly_amount, monthly_amount)

		return SizeInfo(
			size=offer["name"],
			cpu_count=sum(cpu["core_count"] for cpu in offer.get("cpus", [])),
			memory_mb=sum(memory["capacity"] for memory in offer.get("memories", [])) // 1_048_576,
			disk_gib=sum(disk["capacity"] for disk in offer.get("disks", [])) // 1_073_741_824,
			hourly_pricing_usd_cents=hourly_amount,
			monthly_pricing_usd_cents=monthly_amount,
			provider_metadata=metadata,
		)

	@staticmethod
	def _accepted_image(image: Mapping) -> ImageInfo | None:
		"""Return an ImageInfo for an accepted OS/version, or None to skip it."""
		os_name = image.get("name") or ""
		version = image.get("version")
		version = version.split()[0] if isinstance(version, str) else ""
		if version not in ACCEPTED_OS_VERSIONS.get(os_name, ()):
			return None

		metadata = dict(image)
		metadata["os_id"] = image.get("id")
		return ImageInfo(
			image=f"{os_name}_{version}", os=os_name, version=version, provider_metadata=metadata
		)

	@staticmethod
	def _money_to_usd_cents(money: object) -> int | None:
		if not isinstance(money, Mapping) or not isinstance(money.get("units"), int):
			return None
		return round(
			(money["units"] + (money.get("nanos") or 0) / 1_000_000_000) * ScalewayProvider.price_scale
		)

	@staticmethod
	def _fill_missing_prices(
		hourly_amount: int | None, monthly_amount: int | None
	) -> tuple[int | None, int | None]:
		if hourly_amount is None and monthly_amount is not None:
			hourly_amount = round(monthly_amount / 720)
		elif monthly_amount is None and hourly_amount is not None:
			monthly_amount = hourly_amount * 720
		return hourly_amount, monthly_amount

	@staticmethod
	def _key_identity(public_key: str | None) -> str:
		return " ".join((public_key or "").split()[:2])

	def _request(self, method: str, path: str, **kwargs: object) -> dict[str, Any]:
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
