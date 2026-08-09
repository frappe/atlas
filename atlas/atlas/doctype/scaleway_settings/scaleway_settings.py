import dataclasses

import frappe
from frappe import _
from frappe.model.document import Document


class ScalewaySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		billing: DF.Literal["hourly", "monthly"]
		organization_id: DF.Data | None
		project_id: DF.Data
		secret_key: DF.Password
		ssh_key_id: DF.Data | None
		zone: DF.Data
	# end: auto-generated types

	@frappe.whitelist()
	def setup(
		self,
		secret_key: str,
		project_id: str,
		zone: str,
		default_size: str | None = None,
		default_image: str | None = None,
		organization_id: str | None = None,
		billing: str = "hourly",
		ssh_key_id: str | None = None,
	) -> None:
		"""Explicit, idempotent setter for Scaleway Settings (the contract).

		`zone` is the Scaleway Elastic Metal zone (e.g. "fr-par-2") — the vendor's own
		operating zone, NOT `Atlas Settings.region`. Scaleway operates in many zones;
		this names the one Atlas provisions in.

		LOAD-BEARING ORDERING (kept from bootstrap's `_seed_scaleway_settings`): unlike
		DO, Scaleway's `discover()` is the ONLY source of the per-zone `offer_id` /
		`os_id` UUIDs `provision()` reads — so it must run and fail loud if it fails,
		seeding the catalog rows BEFORE any default is marked. discover() also hints a
		default (cheapest offer / Ubuntu LTS) into the empty slot.

		`default_size` / `default_image` are OPTIONAL vendor-native slugs (the operator's
		`atlas_scw_default_*` config keys). When given, they are verified to exist in the
		freshly-discovered catalog (a casing typo — EM-A610R-NVME vs -NVMe — is an
		operator mistake worth surfacing now, not at provision time) and marked
		`is_default`, overriding the discover() hint. The operator can flip the default on
		the Provider Size/Image list anytime.

		The IAM SSH key is uploaded at provision time, so `ssh_key_id` is optional: the
		provider registers `Atlas Settings.ssh_public_key` with IAM if it is unset.

		Writes through `doc.save()` so the `secret_key` Password goes through the normal
		ORM path (`_save_passwords` encrypts to `__Auth` AND stamps the field placeholder,
		so the desk form shows it as set)."""
		from atlas.atlas.providers.scaleway import ScalewayProvider
		from atlas.atlas.provisioning import upsert_catalog
		from atlas.atlas.setup_catalog import set_default

		self.zone = zone
		self.project_id = project_id
		if organization_id:
			self.organization_id = organization_id
		self.billing = billing or "hourly"
		if ssh_key_id:
			self.ssh_key_id = ssh_key_id
		self.secret_key = secret_key
		self.save(ignore_permissions=True)
		# Persist the creds/zone the discover() below reads.
		# nosemgrep: frappe-manual-commit -- setup setter: discover() authenticates with the secret_key just written; commit so it (and any retry) reads it.
		frappe.db.commit()

		# Load-bearing discover — let it propagate so a bad key/zone fails loudly here,
		# not at the first opaque provision(). Seeds the catalog + hints a default.
		upsert_catalog("Scaleway", ScalewayProvider().discover())
		# nosemgrep: frappe-manual-commit -- setup setter: persist discovered catalog rows so a default size/image can be marked below.
		frappe.db.commit()

		# An explicit operator/config slug wins over the discover() hint. Verify it
		# exists in the discovered catalog (casing matters), then mark it the default.
		if default_size:
			size_name = f"Scaleway/{default_size}"
			if not frappe.db.exists("Provider Size", size_name):
				frappe.throw(
					_(
						"Provider Size {0} not in the discovered catalog — check default_size against the "
						"live zone offers (casing matters, e.g. EM-A610R-NVME)."
					).format(size_name)
				)
			set_default("Provider Size", "Scaleway", default_size)
		if default_image:
			image_name = f"Scaleway/{default_image}"
			if not frappe.db.exists("Provider Image", image_name):
				frappe.throw(
					_(
						"Provider Image {0} not in the discovered catalog — check default_image against the "
						"live zone OS list (casing matters, e.g. Ubuntu_24.04)."
					).format(image_name)
				)
			set_default("Provider Image", "Scaleway", default_image)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Scaleway using the Scaleway provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("Scaleway").authenticate()
		return dataclasses.asdict(result)
