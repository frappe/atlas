import dataclasses

import frappe
from frappe.model.document import Document

from atlas.atlas.setup_catalog import (
	ensure_provider_image,
	ensure_provider_size,
	set_default,
)


class DigitalOceanSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_token: DF.Password
		region: DF.Data
		ssh_key_id: DF.Data | None
	# end: auto-generated types

	@frappe.whitelist()
	def setup(
		self,
		api_token: str,
		region: str,
		default_size: str | None = None,
		default_image: str | None = None,
		ssh_key_id: str | None = None,
	) -> None:
		"""Explicit, idempotent setter for DigitalOcean Settings (the contract).

		`region` here is the DigitalOcean API region (e.g. "blr1") — the vendor's own
		operating region, NOT `Atlas Settings.region`. DO operates in many regions;
		this names the one Atlas provisions DO droplets in.

		`default_size` / `default_image` are OPTIONAL vendor-native slugs (the operator's
		`atlas_do_default_*` config keys). When given, the named Provider Size / Provider
		Image rows are seeded and marked `is_default` — the operator choice overrides the
		provider's discover() hint. When omitted, the best-effort `discover()` below seeds
		the catalog and hints its own default into the empty slot (DO: s-2vcpu-4gb-intel /
		ubuntu-24-04-x64); the operator can flip the default on the Provider Size/Image
		list anytime.

		`ssh_key_id` is optional: if omitted, the provider resolves it at provision time
		by querying the DO account for a matching public key and uploading one if absent,
		then caching the id here for subsequent provisions.

		Writes through `doc.save()` so every field — the `api_token` Password included —
		goes through the normal ORM path: `_save_passwords` encrypts the token into
		`__Auth` AND stamps the field placeholder, so the desk form shows it as set.
		Idempotent: re-running just overwrites the Single."""
		self.region = region
		if ssh_key_id:
			self.ssh_key_id = ssh_key_id
		self.api_token = api_token
		self.save(ignore_permissions=True)

		# Seed the wider catalog so the Refresh Catalog button starts from real data.
		# Best-effort — same as bootstrap (DO's discover is gravy, unlike Scaleway's
		# load-bearing discover). discover() also hints its default into an empty slot.
		from atlas.atlas.providers.digitalocean import DigitalOceanProvider
		from atlas.atlas.provisioning import upsert_catalog

		try:
			upsert_catalog("DigitalOcean", DigitalOceanProvider().discover())
		except Exception as exception:
			frappe.log_error(f"DigitalOcean catalog discover() failed during setup: {exception}")

		# An explicit operator/config slug wins over the discover() hint: seed the row
		# (in case discover() failed above) and mark it the lone default.
		if default_size:
			ensure_provider_size("DigitalOcean", default_size)
			set_default("Provider Size", "DigitalOcean", default_size)
		if default_image:
			ensure_provider_image("DigitalOcean", default_image)
			set_default("Provider Image", "DigitalOcean", default_image)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping DigitalOcean using the DigitalOcean provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("DigitalOcean").authenticate()
		return dataclasses.asdict(result)
