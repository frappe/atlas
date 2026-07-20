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
from atlas.atlas.providers.fake import require_developer_mode


class AtlasSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		default_bench_snapshot: DF.Link | None
		default_user_image: DF.Link | None
		dns_provider_type: DF.Literal["", "Route53", "PowerDNS", "Cloudflare"]
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
		key path is expanduser'd. The file is only *needed* at provision time (the
		controller SSHes hosts with it), so a missing file here is a soft warning, NOT
		a hard error — config must persist even when the wizard runs on a box that
		isn't the eventual controller. `ssh_public_key` is derived from the file via
		`ssh-keygen -y` when omitted and the file is readable (load-bearing for
		self-serve — the Site clone path reads the public key off this Single)."""
		if provider_type not in ("DigitalOcean", "Scaleway", "Self-Managed", "Fake"):
			frappe.throw(
				_("provider_type must be DigitalOcean, Scaleway, Self-Managed or Fake, got {0}").format(
					provider_type
				)
			)
		if not region:
			frappe.throw(_("region is required — this Atlas's single region."))

		expanded = os.path.expanduser(ssh_private_key_path)
		key_present = os.path.isfile(expanded)
		if not key_present:
			# Don't abort: warn and persist. A hard throw here used to roll the whole
			# setup stage back (taking the not-yet-written vendor credentials with it).
			frappe.msgprint(
				_(
					"SSH private key {0} is not a file on this host yet. Saved anyway — "
					"it must exist on the controller before you provision a Server."
				).format(expanded),
				title=_("SSH key not found"),
				indicator="orange",
			)

		frappe.db.set_single_value("Atlas Settings", "region", region, update_modified=False)
		frappe.db.set_single_value("Atlas Settings", "provider_type", provider_type, update_modified=False)
		frappe.db.set_single_value("Atlas Settings", "ssh_private_key_path", expanded, update_modified=False)

		# Derive the public key only when the operator didn't supply one AND the
		# private key is actually readable here; otherwise leave it for a later re-run
		# (or the explicit field) rather than failing the save.
		public_key = ssh_public_key or (self._derive_public_key(expanded) if key_present else None)
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
	def bake_golden_image(self, force: bool = False) -> str:
		"""Bake Golden Image button — the desk equivalent of bootstrap's
		`bake_golden_image` step. Resolves the newest Active Server (the same target
		`run_self_serve` bakes on), then enqueues the bake as a `long` background job:
		building bench in a guest then snapshotting takes minutes, so it can't run in
		the web worker. Wires `Atlas Settings.default_bench_snapshot` when done.

		Returns the Server name the bake was enqueued against. `force=True` re-bakes
		even if an Available golden snapshot is already configured."""
		frappe.only_for("System Manager")
		server_name = _newest_active_server()
		frappe.enqueue(
			"atlas.bootstrap.bake_golden_image",
			queue="long",
			timeout=3600,
			server_name=server_name,
			force=frappe.parse_json(force) if isinstance(force, str) else force,
		)
		return server_name

	@frappe.whitelist()
	def ensure_proxy(self) -> str:
		"""Ensure Proxy button — the desk equivalent of bootstrap's `ensure_proxy`
		step. Reads the region + wildcard domain off the active Root Domain (the same
		source `run_self_serve` reads via the TLS config), resolves the newest Active
		Server, then enqueues the proxy stand-up (provision VM → build nginx+Lua stack
		→ attach a reserved IPv4) as a `long` job. Idempotent server-side: a Running
		proxy VM in the region is reused rather than provisioning a second.

		Returns the Server name the proxy was enqueued against."""
		frappe.only_for("System Manager")
		server_name = _newest_active_server()
		region, domain = _proxy_region_and_domain()
		frappe.enqueue(
			"atlas.bootstrap.ensure_proxy",
			queue="long",
			timeout=1800,
			server_name=server_name,
			region=region,
			domain=domain,
		)
		return server_name

	@frappe.whitelist()
	def generate_demo_data(self, reset: bool = False) -> str:
		"""Generate Demo Data button (Fake provider only). Enqueue a `long` job that
		stands up the realistic, varied demo fleet via `atlas.atlas.demo.run` — every
		Server / Virtual Machine status + feature, snapshots, Reserved IPs, and
		back-dated Tasks — built on the Fake provider through the real controllers, so
		the operator can explore the whole lifecycle in Desk with no real cloud.
		Idempotent; `reset=True` wipes the Fake fleet first. `developer_mode`-gated."""
		frappe.only_for("System Manager")
		require_developer_mode()
		if self.provider_type != "Fake":
			frappe.throw(_("Generate Demo Data is only available while the active provider is Fake."))
		frappe.enqueue(
			"atlas.atlas.demo.run",
			queue="long",
			timeout=1800,
			reset=frappe.parse_json(reset) if isinstance(reset, str) else reset,
		)
		return self.provider_type

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

	@frappe.whitelist()
	def discover_reserved_ips(self) -> list[dict]:
		"""Discover Reserved IPs button. List the active vendor's reserved IPs
		(fleet-wide) and, for each, resolve the Server it maps to by droplet binding.
		The recovery path after the Reserved IP rows are gone (a server reset dropped
		them) — the vendor still holds the IPs. Read-only; only `import_reserved_ips`
		writes."""
		return provisioning.discover_reserved_ips(self.provider_type)

	@frappe.whitelist()
	def import_reserved_ips(self, ip_addresses: list[str] | str) -> dict:
		"""Import the picked vendor reserved IPs as Reserved IP rows, auto-mapping each
		to its Server by droplet binding. Idempotent: an already-modeled address is
		skipped. The dialog posts `ip_addresses` as a JSON string, so parse it."""
		ip_addresses = frappe.parse_json(ip_addresses)
		return provisioning.import_reserved_ips(self.provider_type, ip_addresses)


def _newest_active_server() -> str:
	"""The newest Active Server — the target the bake / proxy desk buttons act on,
	mirroring bootstrap's `_existing_active_server` (`run_self_serve` bakes + proxies
	on the server it stood up). Throws if none exists so the operator sees a clear
	"provision a Server first" instead of an enqueued job that fails later."""
	rows = frappe.get_all(
		"Server", filters={"status": "Active"}, pluck="name", order_by="creation desc", limit=1
	)
	if not rows:
		frappe.throw(_("No Active Server. Provision one first, then bake / stand up the proxy."))
	return rows[0]


def _proxy_region_and_domain() -> tuple[str, str]:
	"""The region + wildcard domain the proxy fronts, read off the active Root Domain
	(bootstrap reads the same pair from its TLS config). Throws if no Root Domain
	exists — the proxy serves a region's wildcard, so the TLS layer must be seeded
	first (create a Root Domain, the desk equivalent of `ensure_tls_layer`)."""
	rows = frappe.get_all("Root Domain", fields=["domain", "region"], order_by="creation desc", limit=1)
	if not rows:
		frappe.throw(
			_("No Root Domain. Create one (the region's wildcard zone) before standing up its proxy.")
		)
	return rows[0].region, rows[0].domain
