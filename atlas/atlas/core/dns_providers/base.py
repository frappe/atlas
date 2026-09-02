from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

import frappe

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class DnsProvider(ABC):
	"""Base class for providers registered with Atlas Settings."""

	provider_type: ClassVar[str]
	credential_fields: ClassVar[tuple[str, ...]]

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		self.settings: AtlasSettings = settings or frappe.get_single("Atlas Settings")

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
	def upsert_record(self, record_type: str, name: str, values: list[str], ttl: int = 300) -> None:
		"""Create the DNS record if missing, or update it to match the given values."""
		...

	@abstractmethod
	def remove_record(self, record_type: str, name: str) -> None:
		"""Remove the DNS record for the name and type, if it exists."""
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
