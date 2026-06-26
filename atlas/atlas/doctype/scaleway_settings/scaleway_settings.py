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

		Writes through `doc.save()` so the `secret_key` Password goes through the normal
		ORM path (`_save_passwords` encrypts to `__Auth` AND stamps the field placeholder,
		so the desk form shows it as set). Two saves bracket the load-bearing discover:
		the first persists the creds/zone the discover authenticates with; the second sets
		the default Links once the discovered catalog rows exist. The first save sets
		`ignore_mandatory` because the `reqd` default Links can't exist until the discover
		below has run; the second save validates them fully. The second save re-runs
		`_save_passwords`, but `secret_key` is now the dummy placeholder, so it leaves
		`__Auth` untouched (the encrypted secret survives)."""
		from atlas.atlas.providers.scaleway import ScalewayProvider
		from atlas.atlas.provisioning import upsert_catalog

		self.zone = zone
		self.project_id = project_id
		if organization_id:
			self.organization_id = organization_id
		self.billing = billing or "hourly"
		if ssh_key_id:
			self.ssh_key_id = ssh_key_id
		self.secret_key = secret_key
		# The reqd default Links aren't known until the discover() below — skip mandatory
		# on this creds-only save; the second save validates them.
		self.flags.ignore_mandatory = True
		self.save(ignore_permissions=True)
		self.flags.ignore_mandatory = False
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
		self.default_size = size_name
		self.default_image = image_name
		self.save(ignore_permissions=True)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Scaleway using the Scaleway provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("Scaleway").authenticate()
		return dataclasses.asdict(result)
