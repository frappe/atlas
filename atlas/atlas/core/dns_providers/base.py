from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ipaddress import ip_address
from typing import TYPE_CHECKING, ClassVar

import frappe

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.mesh_address import MESH_NETWORK

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

# Regional services that tenant 0 may publish. Metal and Pilot dial `internal-<label>` over the mesh.
SERVICE_RECORD_LABELS = ("s3-svc", "s3-admin-svc", "telemetry-svc", "telemetry-read-svc")
LABEL_PATTERN = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
HOST_NAME_PATTERN = re.compile(rf"(?=.{{1,253}}$)(?:{LABEL_PATTERN}\.)*{LABEL_PATTERN}\.?")
SERVICE_NAME_PATTERN = (
	rf"(?:(?P<sublabel>{LABEL_PATTERN})\.)?(?P<internal>internal-)?(?P<service>[a-z0-9-]+)\."
)
# Printable ASCII without `"` and `\`, so that a value needs no escaping inside quotes.
TXT_VALUE_PATTERN = re.compile(r"[ !#-\[\]-~]{1,255}")


@dataclass
class DnsRecord:
	"""One record set. A record with an `identifier` is one health-checked member of its name."""

	name: str
	record_type: str
	values: list[str]
	ttl: int = 300
	identifier: str | None = None
	health_check_id: str | None = None


@dataclass
class HealthCheck:
	"""An HTTPS check on port 443 that asks `host` and `path` at one address."""

	host: str
	path: str = "/"


class DnsProviderError(Exception):
	"""Raised when the DNS provider rejects a request."""


class DnsProvider(ABC):
	"""Base class for providers registered with Atlas Settings."""

	provider_type: ClassVar[str]
	credential_fields: ClassVar[tuple[str, ...]]

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		self.settings: AtlasSettings = settings or frappe.get_single("Atlas Settings")
		self.domain_name = self.settings.wildcard_domain.removeprefix("*.")

	@abstractmethod
	def bootstrap(self) -> None:
		"""Set up the provider resources required by Atlas."""
		...

	@abstractmethod
	def validate_settings(self) -> None:
		"""Check the provider fields when Atlas Settings validates."""
		...

	@abstractmethod
	def validate_credentials(self) -> bool:
		"""Use before provider operations to check the configured credentials."""
		...

	@abstractmethod
	def create_zone(self, domain: str) -> str:
		"""Return the existing Atlas DNS zone ID, or create one."""
		...

	@abstractmethod
	def find_public_zone_id(self, domain: str) -> str | None:
		"""Return the ID of an existing public zone for the exact domain."""
		...

	@abstractmethod
	def get_records(
		self, name: str, record_type: str | None = None, identifier: str | None = None
	) -> list[DnsRecord]:
		"""Return the record sets of the name, filtered by type and identifier when given."""
		...

	@abstractmethod
	def upsert_records(self, records: list[DnsRecord], replaced: list[DnsRecord] | None = None) -> None:
		"""Create or update the records, and remove the replaced records in the same change."""
		...

	@abstractmethod
	def remove_records(self, records: list[DnsRecord]) -> None:
		"""Remove the records in one change. An empty list does nothing."""
		...

	def get_health_check(self, health_check_id: str) -> HealthCheck | None:
		"""Return what the health check asks, or None when it is gone."""
		raise NotImplementedError(f"{self.provider_type} has no health checks.")

	def create_health_check(self, address: str, health_check: HealthCheck) -> str:
		"""Create a health check on the address and return its ID."""
		raise NotImplementedError(f"{self.provider_type} has no health checks.")

	def remove_health_check(self, health_check_id: str) -> None:
		"""Remove one health check. A missing check is not an error."""
		raise NotImplementedError(f"{self.provider_type} has no health checks.")

	def upsert_dns_records(
		self,
		name: str,
		record_type: str,
		values: list[str],
		ttl: int,
		health_check: HealthCheck | None = None,
	) -> None:
		"""Validate a tenant-0 request, then set the records of a service name or one label under it. Atlas code calls `upsert_records` directly, without these checks."""
		self._validate_dns_request(name, record_type, values, health_check)

		existing = self.get_records(name)
		records: list[DnsRecord] = []
		try:
			if health_check:
				reusable = self._get_reusable_health_checks(existing, record_type, health_check)
				for address in values:
					health_check_id = reusable.get(address) or self.create_health_check(address, health_check)
					records.append(DnsRecord(name, record_type, [address], ttl, address, health_check_id))
			else:
				records.append(DnsRecord(name, record_type, values, ttl))

			self.upsert_records(records, self._get_replaced_records(existing, record_type, records))
		finally:
			self._remove_unused_health_checks(name, existing + records)

	def remove_dns_records(self, name: str) -> None:
		"""Validate a tenant-0 request, then remove every record of the name and its health checks."""
		self._validate_dns_name(name)
		records = self.get_records(name)
		try:
			self.remove_records(records)
		finally:
			self._remove_unused_health_checks(name, records)

	@staticmethod
	def _get_replaced_records(
		existing: list[DnsRecord], record_type: str, records: list[DnsRecord]
	) -> list[DnsRecord]:
		"""Return the existing records that the new records do not overwrite but must replace. A and AAAA replace each other, and a CNAME cannot share its name with a TXT record."""
		group = ("A", "AAAA") if record_type in ("A", "AAAA") else ("TXT", "CNAME")
		upserted = {(record.record_type, record.identifier) for record in records}
		return [
			record
			for record in existing
			if record.record_type in group and (record.record_type, record.identifier) not in upserted
		]

	def _get_reusable_health_checks(
		self, existing: list[DnsRecord], record_type: str, health_check: HealthCheck
	) -> dict[str, str]:
		"""Map each member address to its health check, when the check asks the same host and path."""
		return {
			record.identifier: record.health_check_id
			for record in existing
			if record.record_type == record_type
			and record.identifier
			and record.health_check_id
			and self.get_health_check(record.health_check_id) == health_check
		}

	def _remove_unused_health_checks(self, name: str, records: list[DnsRecord]) -> None:
		"""Remove the checks of these records that no record of the name uses now. Reading the name again keeps a check when a failed change reached the provider anyway, such as after a timeout."""
		health_check_ids = {record.health_check_id for record in records if record.health_check_id}
		if not health_check_ids:
			return

		used = {record.health_check_id for record in self.get_records(name)}
		for health_check_id in sorted(health_check_ids - used):
			self.remove_health_check(health_check_id)

	def _validate_dns_request(
		self, name: str, record_type: str, values: list[str], health_check: HealthCheck | None
	) -> None:
		is_internal = self._validate_dns_name(name, record_type)
		self._validate_dns_values(name, record_type, values, is_internal)
		if health_check:
			self._validate_health_check(name, record_type, health_check, is_internal)

	def _validate_dns_name(self, name: str, record_type: str | None = None) -> bool:
		"""Return whether the name is an `internal-` name. Atlas owns every other name in the zone, such as `proxy.<domain>` and `*.<domain>`."""
		match = re.fullmatch(SERVICE_NAME_PATTERN + re.escape(self.domain_name), name)
		if not match or match["service"] not in SERVICE_RECORD_LABELS:
			raise AtlasUserError(f"{name} is not a service record name under {self.domain_name}.")
		if match["sublabel"] and record_type in ("A", "AAAA"):
			raise AtlasUserError(f"{name} takes only TXT and CNAME records.")
		if not match["sublabel"] and record_type == "CNAME":
			raise AtlasUserError(f"{name} cannot take a CNAME record. Use a name under it.")
		return bool(match["internal"])

	def _validate_dns_values(self, name: str, record_type: str, values: list[str], is_internal: bool) -> None:
		if record_type == "TXT" and not all(TXT_VALUE_PATTERN.fullmatch(value) for value in values):
			raise AtlasUserError(
				"A TXT value takes 1 to 255 printable ASCII characters without quotes or backslashes."
			)
		if record_type == "CNAME" and (len(values) != 1 or not HOST_NAME_PATTERN.fullmatch(values[0])):
			raise AtlasUserError("A CNAME record takes one host name.")
		if record_type in ("A", "AAAA"):
			self._validate_dns_addresses(name, record_type, values, is_internal)

	def _validate_dns_addresses(
		self, name: str, record_type: str, values: list[str], is_internal: bool
	) -> None:
		try:
			addresses = [ip_address(value) for value in values]
		except ValueError as error:
			raise AtlasUserError(str(error)) from error

		version = 4 if record_type == "A" else 6
		if len(set(addresses)) < len(addresses) or {address.version for address in addresses} != {version}:
			raise AtlasUserError(f"An {record_type} record takes distinct IPv{version} addresses.")

		if is_internal:
			if not all(address in MESH_NETWORK for address in addresses):
				raise AtlasUserError(f"{name} needs WG Mesh addresses in {MESH_NETWORK}.")
		elif not all(address.is_global and not address.is_multicast for address in addresses):
			raise AtlasUserError(f"{name} needs public addresses.")

	def _validate_health_check(
		self, name: str, record_type: str, health_check: HealthCheck, is_internal: bool
	) -> None:
		if is_internal or record_type not in ("A", "AAAA"):
			raise AtlasUserError(
				f"{name} cannot take a health check. Only A and AAAA records on a public name can."
			)
		if not HOST_NAME_PATTERN.fullmatch(health_check.host) or not health_check.path.startswith("/"):
			raise AtlasUserError("A health check needs a host name and a path that starts with `/`.")
