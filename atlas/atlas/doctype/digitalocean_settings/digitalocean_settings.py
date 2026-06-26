import dataclasses

import frappe
from frappe.model.document import Document

from atlas.atlas.setup_catalog import ensure_provider_image, ensure_provider_size


class DigitalOceanSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_token: DF.Password
		default_image: DF.Link
		default_size: DF.Link
		region: DF.Data
		ssh_key_id: DF.Data | None
	# end: auto-generated types

	@frappe.whitelist()
	def setup(
		self,
		api_token: str,
		region: str,
		default_size: str,
		default_image: str,
		ssh_key_id: str | None = None,
	) -> None:
		"""Explicit, idempotent setter for DigitalOcean Settings (the contract).

		`region` here is the DigitalOcean API region (e.g. "blr1") — the vendor's own
		operating region, NOT `Atlas Settings.region`. DO operates in many regions;
		this names the one Atlas provisions DO droplets in. `default_size` /
		`default_image` are vendor-native slugs (Atlas prefixes "DigitalOcean/"); the
		Provider Size / Provider Image rows they Link to are seeded here so the Links
		resolve. A best-effort `discover()` then upserts the wider live catalog (as
		bootstrap does); a failure there is non-fatal — the named slugs already exist.

		`ssh_key_id` is optional: if omitted, the provider resolves it at provision time
		by querying the DO account for a matching public key and uploading one if absent,
		then caching the id here for subsequent provisions.

		Writes through `doc.save()` so every field — the `api_token` Password included —
		goes through the normal ORM path: `_save_passwords` encrypts the token into
		`__Auth` AND stamps the field placeholder, so the desk form shows it as set.
		Idempotent: re-running just overwrites the Single."""
		ensure_provider_size("DigitalOcean", default_size)
		ensure_provider_image("DigitalOcean", default_image)

		self.region = region
		self.default_size = f"DigitalOcean/{default_size}"
		self.default_image = f"DigitalOcean/{default_image}"
		if ssh_key_id:
			self.ssh_key_id = ssh_key_id
		self.api_token = api_token
		self.save(ignore_permissions=True)

		# Seed the wider catalog so the Refresh Catalog button starts from real data,
		# not just the named slugs. Best-effort — same as bootstrap (DO's discover is
		# gravy on top of the named slugs, unlike Scaleway's load-bearing discover).
		from atlas.atlas.providers.digitalocean import DigitalOceanProvider
		from atlas.atlas.provisioning import upsert_catalog

		try:
			upsert_catalog("DigitalOcean", DigitalOceanProvider().discover())
		except Exception as exception:
			frappe.log_error(f"DigitalOcean catalog discover() failed during setup: {exception}")

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping DigitalOcean using the DigitalOcean provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("DigitalOcean").authenticate()
		return dataclasses.asdict(result)
