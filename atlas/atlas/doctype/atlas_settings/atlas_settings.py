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
import os
import subprocess
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import provisioning


class AtlasSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		default_bench_snapshot: DF.Link | None
		default_user_image: DF.Link | None
		dns_provider_type: DF.Literal["", "Route53", "Cloudflare"]
		fail_scripts: DF.SmallText | None
		overprovision_factor: DF.Float
		provider_type: DF.Literal["", "DigitalOcean", "Scaleway", "Self-Managed", "Fake"]
		region: DF.Data
		ssh_private_key_path: DF.Data
		ssh_public_key: DF.LongText | None
		tcp_port_pool: DF.Data | None
		tls_provider_type: DF.Literal["", "Let's Encrypt", "ZeroSSL", "Self-Managed"]
	# end: auto-generated types

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
	def setup(
		self,
		provider_type: str,
		ssh_private_key_path: str,
		region: str,
		ssh_public_key: str | None = None,
		default_bench_snapshot: str | None = None,
	) -> None:
		"""Explicit, idempotent setter for the vendor-agnostic config (the contract).

		`region` is THIS Atlas's single region (the source of truth read at runtime by
		`placement.atlas_region`) — NOT a vendor API region. A provider operates in
		many regions; Atlas pins one. The vendor's own API region/zone lives on its
		Settings (`DigitalOcean Settings.region`, `Scaleway Settings.zone`) and is set
		by that vendor's `setup()`, independently of this value.

		Writes via `set_single_value` (NOT `doc.save()`) so it stays re-runnable. The
		key path is expanduser'd and must point at a real file; `ssh_public_key` is
		derived from it via `ssh-keygen -y` when omitted (load-bearing for self-serve —
		the Site clone path reads the public key off this Single)."""
		if provider_type not in ("DigitalOcean", "Scaleway", "Self-Managed", "Fake"):
			frappe.throw(
				_("provider_type must be DigitalOcean, Scaleway, Self-Managed or Fake, got {0}").format(
					provider_type
				)
			)
		if not region:
			frappe.throw(_("region is required — this Atlas's single region."))

		expanded = os.path.expanduser(ssh_private_key_path)
		if not os.path.isfile(expanded):
			frappe.throw(_("ssh_private_key_path expands to {0}, which is not a file").format(expanded))

		frappe.db.set_single_value("Atlas Settings", "region", region, update_modified=False)
		frappe.db.set_single_value("Atlas Settings", "provider_type", provider_type, update_modified=False)
		frappe.db.set_single_value("Atlas Settings", "ssh_private_key_path", expanded, update_modified=False)

		public_key = ssh_public_key or self._derive_public_key(expanded)
		if public_key:
			frappe.db.set_single_value("Atlas Settings", "ssh_public_key", public_key, update_modified=False)
		if default_bench_snapshot:
			frappe.db.set_single_value(
				"Atlas Settings", "default_bench_snapshot", default_bench_snapshot, update_modified=False
			)

	@staticmethod
	def _derive_public_key(private_key_path: str) -> str | None:
		"""Derive the OpenSSH public key from a private key via `ssh-keygen -y`, or
		None if the key can't be read (mirrors bootstrap's `_resolve_fleet_public_key`)."""
		result = subprocess.run(["ssh-keygen", "-y", "-f", private_key_path], capture_output=True, text=True)
		return result.stdout.strip() if result.returncode == 0 else None

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
