from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

import frappe

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


@dataclass(frozen=True, slots=True)
class SizeInfo:
	"""Vendor server size data."""

	size: str
	cpu_count: int
	memory_mb: int
	disk_gib: int
	hourly_pricing_usd_cents: int | None
	monthly_pricing_usd_cents: int | None
	provider_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ImageInfo:
	"""Vendor server OS image data."""

	image: str
	os: str
	version: str
	provider_metadata: Mapping[str, Any] | None = None


# The only OS/version combinations Server Image accepts. A provider's
# fetch_server_images() must filter its catalog against this before returning.
ACCEPTED_OS_VERSIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
	{
		"Ubuntu": ("22.04", "24.04", "26.04"),
		"Debian": ("11", "12", "13"),
	}
)


class ServerProvider(ABC):
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
	def create_private_network(self, cidr: str) -> str:
		"""Return the existing private network ID, or create one."""
		...

	@abstractmethod
	def create_ssh_key(self, public_key: str) -> str:
		"""Return the existing SSH key ID, or upload the Atlas public key."""
		...

	@abstractmethod
	def fetch_server_sizes(self) -> tuple[SizeInfo, ...]:
		"""Return the server sizes available from the vendor."""
		...

	@abstractmethod
	def fetch_server_images(self) -> tuple[ImageInfo, ...]:
		"""Return the server images available from the vendor."""
		...

	def sync_provider_sizes(self) -> None:
		"""Sync provider sizes with Server Size records."""
		for size in self.fetch_server_sizes():
			name = f"{self.provider_type}/{size.size}"
			metadata_json = frappe.as_json(size.provider_metadata)
			if frappe.db.exists("Server Size", name):
				document = frappe.get_doc("Server Size", name)
				if (
					document.cpu_count == size.cpu_count
					and document.memory_mb == size.memory_mb
					and document.disk_gib == size.disk_gib
					and document.hourly_pricing_usd_cents == size.hourly_pricing_usd_cents
					and document.monthly_pricing_usd_cents == size.monthly_pricing_usd_cents
					and document.provider_metadata == metadata_json
				):
					continue

				document.cpu_count = size.cpu_count
				document.memory_mb = size.memory_mb
				document.disk_gib = size.disk_gib
				document.hourly_pricing_usd_cents = size.hourly_pricing_usd_cents
				document.monthly_pricing_usd_cents = size.monthly_pricing_usd_cents
				document.provider_metadata = metadata_json
				document.save(ignore_permissions=True)
				continue

			frappe.get_doc(
				{
					"doctype": "Server Size",
					"provider_type": self.provider_type,
					"size": size.size,
					"cpu_count": size.cpu_count,
					"memory_mb": size.memory_mb,
					"disk_gib": size.disk_gib,
					"hourly_pricing_usd_cents": size.hourly_pricing_usd_cents,
					"monthly_pricing_usd_cents": size.monthly_pricing_usd_cents,
					"provider_metadata": metadata_json,
				}
			).insert(ignore_permissions=True)

	def sync_provider_images(self) -> None:
		"""Sync provider images with Server Image records."""
		for image in self.fetch_server_images():
			name = f"{self.provider_type}/{image.image}"
			metadata_json = frappe.as_json(image.provider_metadata)
			if frappe.db.exists("Server Image", name):
				document = frappe.get_doc("Server Image", name)
				if document.os != image.os or document.version != image.version:
					frappe.throw(
						f"Server Image {name} has OS/version {document.os}/{document.version}, "
						f"but the provider returned {image.os}/{image.version}."
					)

				if document.provider_metadata == metadata_json:
					continue

				document.provider_metadata = metadata_json
				document.save(ignore_permissions=True)
				continue

			frappe.get_doc(
				{
					"doctype": "Server Image",
					"provider_type": self.provider_type,
					"image": image.image,
					"os": image.os,
					"version": image.version,
					"provider_metadata": metadata_json,
				}
			).insert(ignore_permissions=True)
