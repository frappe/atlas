from __future__ import annotations

from typing import TYPE_CHECKING, override
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from atlas.atlas.core.dns_providers import register
from atlas.atlas.core.dns_providers.base import DnsProvider

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class Route53Error(Exception):
	"""Raised when Route53 rejects a provider request."""


@register
class Route53Provider(DnsProvider):
	provider_type = "Route53"
	credential_fields = ("route53_access_key_id", "route53_access_key_secret")

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
		self.domain_name = self.settings.wildcard_domain.removeprefix("*.")
		self.client = boto3.client(
			"route53",
			aws_access_key_id=self.settings.route53_access_key_id,
			aws_secret_access_key=self.settings.get_password("route53_access_key_secret"),
		)

	@override
	def validate_settings(self) -> None:
		"""Check the wildcard domain."""
		if "." not in self.domain_name:
			raise Route53Error(f"Invalid Atlas wildcard domain: {self.settings.wildcard_domain}")

	@override
	def bootstrap(self) -> None:
		"""Set up the Route53 resources used by Atlas."""
		self.settings.route53_dns_zone_id = self.create_zone(self.domain_name)

		self.settings.is_dns_setup_completed = 1
		self.settings.save()

	@override
	def validate_credentials(self) -> bool:
		"""Return true when the configured credentials can list zones."""
		try:
			self.client.list_hosted_zones(MaxItems="1")
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Unable to validate Route53 credentials: {error}") from error
		return True

	def get_zone_id(self, domain: str) -> str | None:
		"""Return the ID of an existing zone for the domain, or None."""
		try:
			result = self.client.list_hosted_zones_by_name(DNSName=domain, MaxItems="1")
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to list zones for {domain}: {error}") from error

		zones = result.get("HostedZones", [])
		if zones and zones[0]["Name"].rstrip(".") == domain:
			return self._zone_id(zones[0]["Id"])
		return None

	@override
	def create_zone(self, domain: str) -> str:
		"""Return the existing zone ID for the domain, or create one."""
		if self.settings.route53_dns_zone_id:
			self._validate_zone(self.settings.route53_dns_zone_id, domain)
			return self.settings.route53_dns_zone_id

		existing_zone_id = self.get_zone_id(domain)
		if existing_zone_id:
			return existing_zone_id

		try:
			result = self.client.create_hosted_zone(Name=domain, CallerReference=str(uuid4()))
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to create zone for {domain}: {error}") from error
		return self._zone_id(result["HostedZone"]["Id"])

	def _validate_zone(self, zone_id: str, domain: str) -> None:
		try:
			zone = self.client.get_hosted_zone(Id=zone_id)["HostedZone"]
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to fetch zone {zone_id}: {error}") from error

		if zone["Name"].rstrip(".") != domain:
			raise Route53Error(
				f"Zone {zone_id} does not match wildcard domain {self.settings.wildcard_domain}"
			)

	@staticmethod
	def _zone_id(raw_id: str) -> str:
		return raw_id.removeprefix("/hostedzone/")
