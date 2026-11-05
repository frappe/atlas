from __future__ import annotations

from typing import TYPE_CHECKING, override
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from atlas.atlas.core.dns_providers import register
from atlas.atlas.core.dns_providers.base import DnsProvider, DnsProviderError

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
		self.upsert_cname_record(
			f"*.{self.domain_name}",
			f"proxy.{self.domain_name}",
			ttl=3600,
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
	def upsert_record(
		self,
		record_type: str,
		name: str,
		values: list[str],
		ttl: int = 300,
		replaced_type: str | None = None,
	) -> None:
		"""Create or update the record, and remove the `replaced_type` record of the name in one change batch."""
		replaced_record = self._get_record(replaced_type, name) if replaced_type else None
		self._change_record("UPSERT", record_type, name, values, ttl, replaced_record)

	@override
	def remove_record(self, record_type: str, name: str) -> None:
		"""Remove the record for the name and type, if it exists."""
		existing = self._get_record(record_type, name)
		if existing is None:
			return
		values = [record["Value"] for record in existing["ResourceRecords"]]
		self._change_record("DELETE", record_type, name, values, existing["TTL"])

	@override
	def create_https_health_check(self, ip_address: str, domain: str, path: str) -> str:
		"""Create the HTTPS health check for one proxy."""
		try:
			result = self.client.create_health_check(
				CallerReference=str(uuid4()),
				HealthCheckConfig={
					"IPAddress": ip_address,
					"Port": 443,
					"Type": "HTTPS",
					"ResourcePath": path,
					"FullyQualifiedDomainName": domain,
					"RequestInterval": 30,
					"FailureThreshold": 2,
					"EnableSNI": True,
				},
			)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to create health check for {domain}: {error}") from error
		return result["HealthCheck"]["Id"]

	@override
	def remove_health_check(self, health_check_id: str) -> None:
		"""Remove one proxy health check."""
		try:
			self.client.delete_health_check(HealthCheckId=health_check_id)
		except ClientError as error:
			if error.response.get("Error", {}).get("Code") == "NoSuchHealthCheck":
				return
			raise Route53Error(f"Failed to remove health check {health_check_id}: {error}") from error
		except BotoCoreError as error:
			raise Route53Error(f"Failed to remove health check {health_check_id}: {error}") from error

	@override
	def upsert_multivalue_a_record(
		self,
		name: str,
		identifier: str,
		ip_address: str,
		health_check_id: str,
		ttl: int = 120,
	) -> None:
		"""Create or update one health-checked multivalue A record."""
		self._change_multivalue_a_record("UPSERT", name, identifier, ip_address, health_check_id, ttl)

	@override
	def remove_multivalue_a_record(
		self,
		name: str,
		identifier: str,
	) -> None:
		"""Remove one health-checked multivalue A record."""
		record = self._get_multivalue_a_record(name, identifier)
		if record is None:
			return
		try:
			self.client.change_resource_record_sets(
				HostedZoneId=self.create_zone(self.domain_name),
				ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": record}]},
			)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to delete proxy record {name}: {error}") from error

	def _change_record(
		self,
		action: str,
		record_type: str,
		name: str,
		values: list[str],
		ttl: int,
		replaced_record: dict | None = None,
	) -> None:
		zone_id = self.create_zone(self.domain_name)
		record = {
			"Name": name,
			"Type": record_type,
			"TTL": ttl,
			"ResourceRecords": [{"Value": value} for value in values],
		}
		changes = [{"Action": action, "ResourceRecordSet": record}]
		if replaced_record:
			changes.append({"Action": "DELETE", "ResourceRecordSet": replaced_record})

		try:
			self.client.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": changes})
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to {action.lower()} {record_type} record {name}: {error}") from error

	def _change_multivalue_a_record(
		self,
		action: str,
		name: str,
		identifier: str,
		ip_address: str,
		health_check_id: str,
		ttl: int,
	) -> None:
		zone_id = self.create_zone(self.domain_name)
		record = {
			"Name": name,
			"Type": "A",
			"SetIdentifier": identifier,
			"MultiValueAnswer": True,
			"TTL": ttl,
			"ResourceRecords": [{"Value": ip_address}],
			"HealthCheckId": health_check_id,
		}
		try:
			self.client.change_resource_record_sets(
				HostedZoneId=zone_id,
				ChangeBatch={"Changes": [{"Action": action, "ResourceRecordSet": record}]},
			)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to {action.lower()} proxy record {name}: {error}") from error

	def _get_record(self, record_type: str, name: str) -> dict | None:
		zone_id = self.create_zone(self.domain_name)
		try:
			result = self.client.list_resource_record_sets(
				HostedZoneId=zone_id,
				StartRecordName=name,
				StartRecordType=record_type,
				MaxItems="1",
			)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to list records for {name}: {error}") from error

		records = result.get("ResourceRecordSets", [])
		if not records:
			return None

		record = records[0]
		if record["Type"] != record_type or record["Name"].rstrip(".") != name.rstrip("."):
			return None
		return record

	def _get_multivalue_a_record(self, name: str, identifier: str) -> dict | None:
		zone_id = self.create_zone(self.domain_name)
		try:
			result = self.client.list_resource_record_sets(
				HostedZoneId=zone_id,
				StartRecordName=name,
				StartRecordType="A",
				MaxItems="100",
			)
		except (BotoCoreError, ClientError) as error:
			raise Route53Error(f"Failed to list proxy records for {name}: {error}") from error

		return next(
			(
				record
				for record in result.get("ResourceRecordSets", [])
				if record["Name"].rstrip(".") == name.rstrip(".")
				and record["Type"] == "A"
				and record.get("SetIdentifier") == identifier
			),
			None,
		)

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
