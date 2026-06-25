import dataclasses

import frappe
import frappe.utils.password
from frappe import _
from frappe.model.document import Document


class ScalewaySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		billing: DF.Literal["hourly", "monthly"]
		default_image: DF.Link
		default_size: DF.Link
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
		default_size: str,
		default_image: str,
		organization_id: str | None = None,
		billing: str = "hourly",
		ssh_key_id: str | None = None,
	) -> None:
		"""Explicit, idempotent setter for Scaleway Settings (the contract).

		`zone` is the Scaleway Elastic Metal zone (e.g. "fr-par-2") — the vendor's own
		operating zone, NOT `Atlas Settings.region`. Scaleway operates in many zones;
		this names the one Atlas provisions in.

		LOAD-BEARING ORDERING (kept verbatim from bootstrap's `_seed_scaleway_settings`):
		unlike DO, Scaleway's `discover()` is the ONLY source of the per-zone
		`offer_id` / `os_id` UUIDs `provision()` reads — so it must run BEFORE the
		`reqd` default Links are set (the Link targets are the discovered rows), it
		fails loud if it fails, and the named default size/image are verified to exist
		in the freshly-discovered catalog (a casing typo — EM-A610R-NVME vs -NVMe — is
		an operator mistake worth surfacing now, not at provision time).

		The IAM SSH key is uploaded at provision time, so `ssh_key_id` is optional: the
		provider registers `Atlas Settings.ssh_public_key` with IAM if it is unset.

		Writes via `set_single_value` / `set_encrypted_password` (NOT `doc.save()`)."""
		from atlas.atlas.providers.scaleway import ScalewayProvider
		from atlas.atlas.provisioning import upsert_catalog

		frappe.db.set_single_value("Scaleway Settings", "zone", zone, update_modified=False)
		frappe.db.set_single_value("Scaleway Settings", "project_id", project_id, update_modified=False)
		if organization_id:
			frappe.db.set_single_value(
				"Scaleway Settings", "organization_id", organization_id, update_modified=False
			)
		frappe.db.set_single_value("Scaleway Settings", "billing", billing or "hourly", update_modified=False)
		if ssh_key_id:
			frappe.db.set_single_value("Scaleway Settings", "ssh_key_id", ssh_key_id, update_modified=False)
		frappe.utils.password.set_encrypted_password(
			"Scaleway Settings", "Scaleway Settings", secret_key, "secret_key"
		)
		# Persist the creds/zone the discover() below reads before the default Links.
		# nosemgrep: frappe-manual-commit -- setup setter: discover() authenticates with the secret_key just written; commit so it (and any retry) reads it.
		frappe.db.commit()

		# Load-bearing discover BEFORE the reqd default Links — let it propagate so a
		# bad key/zone fails loudly here, not at the first opaque provision().
		upsert_catalog("Scaleway", ScalewayProvider().discover())
		# nosemgrep: frappe-manual-commit -- setup setter: persist discovered catalog rows so the default size/image Link targets exist below.
		frappe.db.commit()

		size_name = f"Scaleway/{default_size}"
		image_name = f"Scaleway/{default_image}"
		if not frappe.db.exists("Provider Size", size_name):
			frappe.throw(
				_(
					"Provider Size {0} not in the discovered catalog — check default_size against the "
					"live zone offers (casing matters, e.g. EM-A610R-NVME)."
				).format(size_name)
			)
		if not frappe.db.exists("Provider Image", image_name):
			frappe.throw(
				_(
					"Provider Image {0} not in the discovered catalog — check default_image against the "
					"live zone OS list (casing matters, e.g. Ubuntu_24.04)."
				).format(image_name)
			)
		frappe.db.set_single_value("Scaleway Settings", "default_size", size_name, update_modified=False)
		frappe.db.set_single_value("Scaleway Settings", "default_image", image_name, update_modified=False)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Scaleway using the Scaleway provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("Scaleway").authenticate()
		return dataclasses.asdict(result)
