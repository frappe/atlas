from __future__ import annotations

from typing import TYPE_CHECKING, override
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from atlas.atlas.core.dns_providers import register
from atlas.atlas.core.dns_providers.base import DnsProvider, DnsProviderError, DnsRecord, HealthCheck

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class Route53Error(DnsProviderError):
	"""Raised when Route53 rejects a provider request."""


@register
class Route53Provider(DnsProvider):
	"""Manage DNS records in one Route 53 hosted zone."""

	provider_type = "Route53"
	credential_fields = ("route53_access_key_id", "route53_access_key_secret")

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
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
		self.upsert_records(
			[DnsRecord(f"*.{self.domain_name}", "CNAME", [f"proxy.{self.domain_name}"], 3600)]
		)

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
	def find_public_zone_id(self, domain: str) -> str | None:
		"""Return the ID of an existing public zone for the exact domain."""
		try:
			result = self.client.list_hosted_zones_by_name(DNSName=domain, MaxItems="100")
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to list zones for {domain}: {error}") from error

		for zone in result.get("HostedZones", []):
			if zone["Name"].rstrip(".") != domain:
				break
			if not zone.get("Config", {}).get("PrivateZone", False):
				return self._zone_id(zone["Id"])

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

	@override
	def get_records(
		self, name: str, record_type: str | None = None, identifier: str | None = None
	) -> list[DnsRecord]:
		"""Return the record sets of the name, filtered by type and identifier when given."""
		records = [self._to_dns_record(record_set) for record_set in self._list_record_sets(name)]
		return [
			record
			for record in records
			if record_type in (None, record.record_type) and identifier in (None, record.identifier)
		]

	@override
	def upsert_records(self, records: list[DnsRecord], replaced: list[DnsRecord] | None = None) -> None:
		"""Create or update the records, and remove the replaced records in the same change batch."""
		changes = [("DELETE", record) for record in replaced or []] + [
			("UPSERT", record) for record in records
		]
		self._change_records(changes)

	@override
	def remove_records(self, records: list[DnsRecord]) -> None:
		"""Remove the records in one change batch. An empty list does nothing."""
		self._change_records([("DELETE", record) for record in records])

	@override
	def get_health_check(self, health_check_id: str) -> HealthCheck | None:
		"""Return what the health check asks, or None when it is gone."""
		try:
			result = self.client.get_health_check(HealthCheckId=health_check_id)
		except ClientError as error:
			if error.response.get("Error", {}).get("Code") == "NoSuchHealthCheck":
				return None
			raise Route53Error(f"Failed to get health check {health_check_id}: {error}") from error
		except BotoCoreError as error:
			raise Route53Error(f"Failed to get health check {health_check_id}: {error}") from error

		configuration = result["HealthCheck"]["HealthCheckConfig"]
		return HealthCheck(
			configuration.get("FullyQualifiedDomainName", ""), configuration.get("ResourcePath", "/")
		)

	@override
	def create_health_check(self, address: str, health_check: HealthCheck) -> str:
		"""Create an HTTPS health check on the address and return its ID."""
		try:
			result = self.client.create_health_check(
				CallerReference=str(uuid4()),
				HealthCheckConfig={
					"IPAddress": address,
					"Port": 443,
					"Type": "HTTPS",
					"ResourcePath": health_check.path,
					"FullyQualifiedDomainName": health_check.host,
					"RequestInterval": 30,
					"FailureThreshold": 2,
					"EnableSNI": True,
				},
			)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to create health check for {health_check.host}: {error}") from error
		return result["HealthCheck"]["Id"]

	@override
	def remove_health_check(self, health_check_id: str) -> None:
		"""Remove one health check. A missing check is not an error."""
		try:
			self.client.delete_health_check(HealthCheckId=health_check_id)
		except ClientError as error:
			if error.response.get("Error", {}).get("Code") == "NoSuchHealthCheck":
				return
			raise Route53Error(f"Failed to remove health check {health_check_id}: {error}") from error
		except BotoCoreError as error:
			raise Route53Error(f"Failed to remove health check {health_check_id}: {error}") from error

	def _change_records(self, changes: list[tuple[str, DnsRecord]]) -> None:
		if not changes:
			return

		batch = [
			{"Action": action, "ResourceRecordSet": self._to_record_set(record)} for action, record in changes
		]
		try:
			self.client.change_resource_record_sets(
				HostedZoneId=self.create_zone(self.domain_name), ChangeBatch={"Changes": batch}
			)
		except (BotoCoreError, ClientError) as error:
			names = ", ".join(sorted({record.name for _, record in changes}))
			raise Route53Error(f"Failed to change the records of {names}: {error}") from error

	def _list_record_sets(self, name: str) -> list[dict]:
		"""Route53 lists record sets in name order, so the name's sets come first from `StartRecordName`. It stores names in lowercase."""
		paginator = self.client.get_paginator("list_resource_record_sets")
		record_sets = []
		try:
			for page in paginator.paginate(
				HostedZoneId=self.create_zone(self.domain_name), StartRecordName=name
			):
				for record_set in page.get("ResourceRecordSets", []):
					if record_set["Name"].rstrip(".") != name.rstrip(".").lower():
						return record_sets
					if "ResourceRecords" in record_set:
						record_sets.append(record_set)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to list records for {name}: {error}") from error
		return record_sets

	@staticmethod
	def _to_dns_record(record_set: dict) -> DnsRecord:
		values = [record["Value"] for record in record_set["ResourceRecords"]]
		if record_set["Type"] == "TXT":
			values = [value.removeprefix('"').removesuffix('"') for value in values]
		return DnsRecord(
			name=record_set["Name"].rstrip("."),
			record_type=record_set["Type"],
			values=values,
			ttl=record_set["TTL"],
			identifier=record_set.get("SetIdentifier"),
			health_check_id=record_set.get("HealthCheckId"),
		)

	@staticmethod
	def _to_record_set(record: DnsRecord) -> dict:
		values = [f'"{value}"' for value in record.values] if record.record_type == "TXT" else record.values
		record_set = {
			"Name": record.name,
			"Type": record.record_type,
			"TTL": record.ttl,
			"ResourceRecords": [{"Value": value} for value in values],
		}
		if record.identifier:
			record_set |= {"SetIdentifier": record.identifier, "MultiValueAnswer": True}
		if record.health_check_id:
			record_set["HealthCheckId"] = record.health_check_id
		return record_set

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
