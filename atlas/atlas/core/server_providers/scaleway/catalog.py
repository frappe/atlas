from __future__ import annotations

from collections.abc import Mapping

import frappe

from atlas.atlas.core.server_providers.base import (
	ACCEPTED_OS_VERSIONS,
	ServerImageData,
	ServerSizeData,
)
from atlas.atlas.core.server_providers.scaleway.client import ScalewayError


class ScalewayCatalog:
	"""Translate Scaleway catalog records into Atlas catalog values."""

	price_scale = 100

	def get_server_sizes(self, offers: object) -> tuple[ServerSizeData, ...]:
		"""Return Atlas sizes for Scaleway Elastic Metal offers."""
		if not isinstance(offers, list) or not all(isinstance(offer, Mapping) for offer in offers):
			raise ScalewayError("Scaleway response has invalid offers")

		sizes: dict[str, ServerSizeData] = {}
		for offer in offers:
			name = offer.get("name")
			if not isinstance(name, str):
				raise ScalewayError("Scaleway offer has no name")
			sizes[name] = self._merge_offer(sizes.get(name), offer)
		return tuple(sizes.values())

	def get_server_images(self, images: object) -> tuple[ServerImageData, ...]:
		"""Return Atlas images for supported Scaleway operating systems."""
		if not isinstance(images, list) or not all(isinstance(image, Mapping) for image in images):
			raise ScalewayError("Scaleway response has invalid OS images")
		return tuple(image for item in images if (image := self._accepted_image(item)) is not None)

	def get_offer_id(self, size_document: object, subscription_period: str) -> str:
		"""Return the offer ID for the selected subscription period."""
		offer = self.offer(
			frappe.parse_json(size_document.provider_metadata or "{}"),
			size_document.name,
			"hourly",
		)
		return self.offer_id(offer, size_document.name, subscription_period)

	@staticmethod
	def offer_id(offer: Mapping, size_name: str, subscription_period: str) -> str:
		"""Return the offer ID for one catalog offer."""
		offer_id = offer.get("monthly_offer_id") if subscription_period == "monthly" else offer.get("id")
		if not isinstance(offer_id, str):
			raise ScalewayError(f"Server Size {size_name} has no {subscription_period} Scaleway offer ID")
		return offer_id

	def get_private_network_option_id(self, size_document: object, subscription_period: str) -> str:
		"""Return the Private Network option ID for the selected subscription period.

		Scaleway gives an option its own ID for each period, and it rejects an
		option that does not match the offer.
		"""
		offer = self.offer(
			frappe.parse_json(size_document.provider_metadata or "{}"),
			size_document.name,
			subscription_period,
		)
		return self.private_network_option_id(offer, size_document.name, subscription_period)

	@staticmethod
	def private_network_option_id(offer: Mapping, size_name: str, subscription_period: str) -> str:
		"""Return the private network option ID for one catalog offer."""
		for option in offer.get("options", []):
			if (
				isinstance(option, Mapping)
				and "private_network" in option
				and isinstance(option.get("id"), str)
			):
				return option["id"]
		raise ScalewayError(f"Server Size {size_name} has no Private Network option")

	def _merge_offer(self, size: ServerSizeData | None, offer: Mapping) -> ServerSizeData:
		metadata = dict(size.provider_metadata) if size else {}
		metadata[offer.get("subscription_period")] = dict(offer)

		hourly_amount = self._money_to_usd_cents(offer.get("price_per_hour"))
		monthly_amount = self._money_to_usd_cents(offer.get("price_per_month"))
		if size:
			if hourly_amount is None:
				hourly_amount = size.hourly_pricing_usd_cents
			if monthly_amount is None:
				monthly_amount = size.monthly_pricing_usd_cents
		hourly_amount, monthly_amount = self._fill_missing_prices(hourly_amount, monthly_amount)

		return ServerSizeData(
			size=offer["name"],
			cpu_count=sum(cpu["core_count"] for cpu in offer.get("cpus", [])),
			memory_mib=sum(memory["capacity"] for memory in offer.get("memories", [])) // 1_048_576,
			disk_gib=sum(disk["capacity"] for disk in offer.get("disks", [])) // 1_073_741_824,
			hourly_pricing_usd_cents=hourly_amount,
			monthly_pricing_usd_cents=monthly_amount,
			provider_metadata=metadata,
		)

	@staticmethod
	def offer(metadata: object, size_name: str, subscription_period: str) -> Mapping:
		"""Return one offer from provider size metadata."""
		offer = metadata.get(subscription_period) if isinstance(metadata, Mapping) else None
		if not isinstance(offer, Mapping):
			raise ScalewayError(f"Server Size {size_name} has no {subscription_period} Scaleway offer")
		return offer

	@staticmethod
	def _accepted_image(image: Mapping) -> ServerImageData | None:
		os_name = image.get("name") or ""
		version = image.get("version")
		version = version.split()[0] if isinstance(version, str) else ""
		if version not in ACCEPTED_OS_VERSIONS.get(os_name, ()):
			return None

		metadata = dict(image)
		metadata["os_id"] = image.get("id")
		return ServerImageData(
			image=f"{os_name}_{version}", os=os_name, version=version, provider_metadata=metadata
		)

	@classmethod
	def _money_to_usd_cents(cls, money: object) -> int | None:
		if not isinstance(money, Mapping) or not isinstance(money.get("units"), int):
			return None
		return round((money["units"] + (money.get("nanos") or 0) / 1_000_000_000) * cls.price_scale)

	@staticmethod
	def _fill_missing_prices(
		hourly_amount: int | None, monthly_amount: int | None
	) -> tuple[int | None, int | None]:
		if hourly_amount is None and monthly_amount is not None:
			hourly_amount = round(monthly_amount / 720)
		elif monthly_amount is None and hourly_amount is not None:
			monthly_amount = hourly_amount * 720
		return hourly_amount, monthly_amount
