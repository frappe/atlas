from __future__ import annotations

import re
from abc import ABC, abstractmethod
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING, ClassVar

import frappe

from atlas.atlas.core.exceptions import AtlasUserError

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

# Labels that regional services may set under the Atlas domain. Metal reads them to reach a service privately.
SERVICE_RECORD_PREFIXES = ("internal-",)
SERVICE_RECORD_TTL = 60
DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


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
	def upsert_record(self, record_type: str, name: str, values: list[str], ttl: int = 300) -> None:
		"""Create the DNS record if missing, or update it to match the given values."""
		...

	@abstractmethod
	def remove_record(self, record_type: str, name: str) -> None:
		"""Remove the DNS record for the name and type, if it exists."""
		...

	@abstractmethod
	def create_https_health_check(self, ip_address: str, domain: str, path: str) -> str:
		"""Create an HTTPS health check and return its ID."""
		...

	@abstractmethod
	def remove_health_check(self, health_check_id: str) -> None:
		"""Remove one health check."""
		...

	@abstractmethod
	def upsert_multivalue_a_record(
		self,
		name: str,
		identifier: str,
		ip_address: str,
		health_check_id: str,
		ttl: int = 120,
	) -> None:
		"""Create or update one health-checked multivalue A record."""
		...

	@abstractmethod
	def remove_multivalue_a_record(
		self,
		name: str,
		identifier: str,
	) -> None:
		"""Remove one health-checked multivalue A record."""
		...

	def upsert_a_record(self, name: str, ip_address: str, ttl: int = 300) -> None:
		"""Create or update an A record that points to the given IP address."""
		self.upsert_record("A", name, [ip_address], ttl)

	def upsert_cname_record(self, name: str, target: str, ttl: int = 300) -> None:
		"""Create or update a CNAME record that points to the given target."""
		self.upsert_record("CNAME", name, [target], ttl)

	def upsert_txt_record(self, name: str, value: str, ttl: int = 300) -> None:
		"""Create or update a TXT record with the given value."""
		self.upsert_record("TXT", name, [f'"{value}"'], ttl)

	def remove_a_record(self, name: str) -> None:
		"""Remove the A record for the name, if it exists."""
		self.remove_record("A", name)

	def remove_cname_record(self, name: str) -> None:
		"""Remove the CNAME record for the name, if it exists."""
		self.remove_record("CNAME", name)

	def remove_txt_record(self, name: str) -> None:
		"""Remove the TXT record for the name, if it exists."""
		self.remove_record("TXT", name)

	def upsert_service_record(self, name: str, address: IPv4Address | IPv6Address) -> None:
		"""Point one regional service name to its private address."""
		self._validate_service_record_name(name)
		if not address.is_private:
			raise AtlasUserError(f"{address} is not a private address.")
		self.upsert_record("A" if address.version == 4 else "AAAA", name, [str(address)], SERVICE_RECORD_TTL)

	def remove_service_record(self, name: str) -> None:
		"""Remove the A and AAAA records of one regional service name."""
		self._validate_service_record_name(name)
		self.remove_record("A", name)
		self.remove_record("AAAA", name)

	def _validate_service_record_name(self, name: str) -> None:
		label, _, domain = name.partition(".")
		if (
			domain != self.domain_name
			or not label.startswith(SERVICE_RECORD_PREFIXES)
			or not DNS_LABEL.fullmatch(label)
		):
			raise AtlasUserError(
				f"{name} must be one label under {self.domain_name} that starts with {', '.join(SERVICE_RECORD_PREFIXES)}."
			)
