"""Atlas Settings — the vendor-agnostic Single, and the home of the provider
buttons the deleted `Provider` DocType used to own.

`get_provider()` ([atlas/atlas/atlas_settings.py](../../atlas_settings.py)) reads
`provider_type` off this Single to pick the compute implementation; the Provision /
Authenticate / Refresh Catalog / Discover Servers buttons delegate to that
implementation through [provisioning.py](../../provisioning.py). There is no
"active row" to flip: switching vendor edits `provider_type`, guarded so it can't
orphan live hosts from their vendor client.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import provisioning


class AtlasSettings(Document):
	def validate(self) -> None:
		self._validate_provider_switch()

	def _validate_provider_switch(self) -> None:
		"""Refuse to change `provider_type` while any non-Archived Server was
		provisioned through a different vendor — switching would orphan a live host
		from the client that can describe / destroy it. This is the Single-world
		equivalent of the old "archive doesn't destroy Servers" promise."""
		original = self.get_doc_before_save()
		if not original or original.provider_type == self.provider_type:
			return
		stranded = frappe.get_all(
			"Server",
			filters={
				"status": ("!=", "Archived"),
				"provider_type": ("not in", ["", self.provider_type]),
			},
			pluck="title",
			limit=5,
		)
		if stranded:
			frappe.throw(
				_(
					"Cannot switch provider_type: {0} non-archived Server(s) were provisioned "
					"through a different vendor (e.g. {1}). Archive them first."
				).format(len(stranded), ", ".join(stranded))
			)

	@frappe.whitelist()
	def authenticate(self) -> dict:
		"""Authenticate button — probe the active vendor's API."""
		import atlas

		result = atlas.get_provider().authenticate()
		return dataclasses.asdict(result)

	@frappe.whitelist()
	def refresh_catalog(self) -> dict:
		"""Refresh Catalog button. Reads the active vendor's catalog and upserts
		Provider Size / Provider Image rows; slugs missing from the new list are
		flipped to enabled=0."""
		import atlas

		capabilities = atlas.get_provider().discover()
		return provisioning.upsert_catalog(self.provider_type, capabilities)

	@frappe.whitelist()
	def provision_server(self, title: str, **dialog_fields: Any) -> str:
		"""Provision Server button. Insert a Server row through the active vendor
		and enqueue bootstrap; returns the new row's UUID name."""
		return provisioning.provision_server(self.provider_type, title, dialog_fields)

	@frappe.whitelist()
	def discover_servers(self) -> list[dict]:
		"""Discover Servers button. List the active vendor's servers (unfiltered) and
		flag which Atlas already models by provider_resource_id. Read-only — inserts
		nothing; only `import_servers` writes."""
		return provisioning.discover_servers(self.provider_type)

	@frappe.whitelist()
	def import_servers(self, resource_ids: list[str] | str) -> dict:
		"""Import the picked vendor servers as Pending Server rows. Idempotent: an
		already-modeled id is skipped, never double-inserted. The dialog posts
		`resource_ids` as a JSON string, so parse it before use."""
		resource_ids = frappe.parse_json(resource_ids)
		return provisioning.import_servers(self.provider_type, resource_ids)
