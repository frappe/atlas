from __future__ import annotations

from abc import ABC, abstractmethod
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING, ClassVar

import frappe

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.mesh_address import MESH_NETWORK

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

# Regional services that tenant 0 may publish. Metal and Pilot dial `internal-<label>` over the mesh.
SERVICE_RECORD_LABELS = ("s3-svc", "s3-admin-svc", "telemetry-svc", "telemetry-read-svc")
INTERNAL_RECORD_PREFIX = "internal-"


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
	def upsert_record(
		self,
		record_type: str,
		name: str,
		values: list[str],
		ttl: int = 300,
		replaced_type: str | None = None,
	) -> None:
		"""Create or update the DNS record, and remove the `replaced_type` record of the name in the same change."""
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

	def upsert_service_record(self, name: str, address: IPv4Address | IPv6Address, ttl: int) -> None:
		"""Point a service name to its public address, or its `internal-` name to its mesh address."""
		if self._is_internal_service_record(name):
			if address not in MESH_NETWORK:
				raise AtlasUserError(f"{name} needs a WG Mesh address in {MESH_NETWORK}.")
		elif not address.is_global or address.is_multicast:
			raise AtlasUserError(f"{name} needs a public address.")
		record_type, replaced_type = ("A", "AAAA") if address.version == 4 else ("AAAA", "A")
		self.upsert_record(record_type, name, [str(address)], ttl, replaced_type)

	def remove_service_record(self, name: str) -> None:
		"""Remove the A and AAAA records of one service name."""
		self._is_internal_service_record(name)
		self.remove_record("A", name)
		self.remove_record("AAAA", name)

	def _is_internal_service_record(self, name: str) -> bool:
		label, _, domain = name.partition(".")
		service = label.removeprefix(INTERNAL_RECORD_PREFIX)
		if domain != self.domain_name or service not in SERVICE_RECORD_LABELS:
			raise AtlasUserError(f"{name} is not a service record name under {self.domain_name}.")
		return service != label
