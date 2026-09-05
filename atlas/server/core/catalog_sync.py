from __future__ import annotations

import frappe

from atlas.atlas.core.server_providers.base import ServerImageData, ServerProvider, ServerSizeData


class CatalogSynchronizer:
	"""Store provider catalog data in Atlas records."""

	def __init__(self, provider: ServerProvider) -> None:
		self.provider = provider

	def sync_server_sizes(self) -> None:
		"""Store the current server sizes from the provider."""
		for size in self.provider.fetch_server_sizes():
			self.save_server_size(size)

	def sync_server_images(self) -> None:
		"""Store the current server images from the provider."""
		for image in self.provider.fetch_server_images():
			self.save_server_image(image)

	def save_server_size(self, size: ServerSizeData) -> None:
		"""Create or update one Server Size record."""
		name = f"{self.provider.provider_type}/{size.size}"
		values = {
			"cpu_count": size.cpu_count,
			"memory_mib": size.memory_mib,
			"disk_gib": size.disk_gib,
			"hourly_pricing_usd_cents": size.hourly_pricing_usd_cents,
			"monthly_pricing_usd_cents": size.monthly_pricing_usd_cents,
			"provider_metadata": frappe.as_json(size.provider_metadata),
		}
		if frappe.db.exists("Server Size", name):
			document = frappe.get_doc("Server Size", name)
			if all(document.get(field) == value for field, value in values.items()):
				return
			document.update(values)
			document.save(ignore_permissions=True)
			return

		frappe.get_doc(
			{
				"doctype": "Server Size",
				"provider_type": self.provider.provider_type,
				"size": size.size,
				**values,
			}
		).insert(ignore_permissions=True)

	def save_server_image(self, image: ServerImageData) -> None:
		"""Create or update one Server Image record."""
		name = f"{self.provider.provider_type}/{image.image}"
		metadata = frappe.as_json(image.provider_metadata)
		if frappe.db.exists("Server Image", name):
			document = frappe.get_doc("Server Image", name)
			if document.provider_metadata == metadata:
				return
			document.provider_metadata = metadata
			document.save(ignore_permissions=True)
			return

		frappe.get_doc(
			{
				"doctype": "Server Image",
				"provider_type": self.provider.provider_type,
				"image": image.image,
				"provider_metadata": metadata,
			}
		).insert(ignore_permissions=True)
