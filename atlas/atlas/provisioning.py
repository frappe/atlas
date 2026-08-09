"""Server provisioning helpers — the behavior the old `Provider` controller owned.

The `Provider` DocType is gone: the active vendor is `Atlas Settings.provider_type`
and a Server carries its own `provider_type` (denormalized at insert). These
helpers are driven from the `Atlas Settings` form's Provision / Discover buttons
and from `bootstrap.py`. They resolve the implementation through the registry
(`providers.for_provider_type`) and never branch on the vendor type themselves.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import frappe

from atlas.atlas import providers
from atlas.atlas.providers.base import (
	Networking,
	ProvisionRequest,
	ServerNetworking,
)


def provision_region() -> str:
	"""The region label that separates this bench's resources in a shared cloud
	account (multiple developers share one DigitalOcean / Scaleway account).

	Just `Atlas Settings.region` — the single source of truth for this Atlas's
	region (see `placement.atlas_region`). `bootstrap.py` seeds it before the first
	provision, so it is available from the very first bootstrap step. Fails loud
	when unset (no `"x"` placeholder): an unconfigured region is an operator
	mistake, not a default."""
	from atlas.atlas.placement import atlas_region

	return atlas_region()


def region_server_title(role: str | None = None) -> str:
	"""Self-describing `Server.title` for a box provisioned into a shared cloud
	account: `x-<region>[-<role>]-<6hex>`.

	The region (see `provision_region`) is the per-developer separator so each
	bench's servers are recognizable in the DigitalOcean / Scaleway console; the
	random suffix avoids title collisions without a count query. `role` tags
	short-lived boxes (e.g. `"e2e"`); omit it for the long-lived bootstrap server.
	`Server.name` stays a UUID — this is only the human label.
	"""
	region = provision_region()
	parts = ["x", region, role, uuid.uuid4().hex[:6]] if role else ["x", region, uuid.uuid4().hex[:6]]
	return "-".join(parts)


def provision_server(provider_type: str, title: str, dialog_fields: dict[str, Any]) -> str:
	"""Insert a Server row and enqueue bootstrap.

	`title` is the user-facing label. The row's `name` is a UUID assigned
	by `Server.autoname()`. The Server's `provider_type` is frozen from the
	active vendor. The vendor's `provision()` may return a partial result —
	the worker fills the rest via `describe()`.
	"""
	import atlas

	if frappe.db.exists("Server", {"title": title}):
		frappe.throw(f"Server with title {title!r} already exists")

	provider_impl = providers.for_provider_type(provider_type)
	ssh_key = atlas.get_ssh_key()

	if provider_type == "Self-Managed":
		prebuilt = ServerNetworking(
			ipv4_address=dialog_fields.get("ipv4_address"),
			ipv6_address=dialog_fields.get("ipv6_address"),
			ipv6_prefix=dialog_fields.get("ipv6_prefix"),
			ipv6_virtual_machine_range=dialog_fields.get("ipv6_virtual_machine_range"),
		)
		for label in ("ipv4_address", "ipv6_address", "ipv6_prefix", "ipv6_virtual_machine_range"):
			if not getattr(prebuilt, label):
				frappe.throw(f"Self-Managed providers require {label}")
		request = ProvisionRequest(
			title=title,
			size="",
			image="",
			ssh_key=ssh_key,
			networking=Networking.DUAL_STACK,
			tags=("atlas", title),
			prebuilt_networking=prebuilt,
		)
		result = provider_impl.provision(request)
		server = frappe.get_doc(
			{
				"doctype": "Server",
				"title": title,
				"provider_type": provider_type,
				"status": "Pending",
				"ipv4_address": result.networking.ipv4_address if result.networking else None,
				"ipv6_address": result.networking.ipv6_address if result.networking else None,
				"ipv6_prefix": result.networking.ipv6_prefix if result.networking else None,
				"ipv6_virtual_machine_range": result.networking.ipv6_virtual_machine_range
				if result.networking
				else None,
			}
		).insert(ignore_permissions=True)
	else:
		# The dialog value wins; otherwise fall back to the catalog row marked
		# `is_default` for this provider_type (the Provision Server modal prefills the
		# same row, so this is just the no-override path). The Fake provider
		# (developer_mode) has no catalog default — it defaults size/image inside
		# provision() — so this resolves to "" and the dialog value carries.
		from atlas.atlas.setup_catalog import default_name

		size = dialog_fields.get("size") or default_name("Provider Size", provider_type)
		image = dialog_fields.get("image") or default_name("Provider Image", provider_type)
		request = ProvisionRequest(
			title=title,
			size=size,
			image=image,
			ssh_key=ssh_key,
			networking=Networking.DUAL_STACK,
			tags=("atlas", title),
		)
		result = provider_impl.provision(request)
		server_doc: dict[str, Any] = {
			"doctype": "Server",
			"title": title,
			"provider_type": provider_type,
			"provider_resource_id": result.provider_resource_id,
			"size": result.size,
			"image": result.image,
			"status": "Pending",
		}
		if result.provider_metadata is not None:
			server_doc["provider_metadata"] = json.dumps(result.provider_metadata)
		server = frappe.get_doc(server_doc).insert(ignore_permissions=True)

	# nosemgrep: frappe-manual-commit -- persist the new Server row before enqueuing finish_provisioning so the background job can find it cross-transaction
	frappe.db.commit()

	frappe.enqueue(
		"atlas.atlas.providers.worker.finish_provisioning",
		queue="long",
		timeout=1800,
		server_name=server.name,
	)
	return server.name


def discover_servers(provider_type: str) -> list[dict]:
	"""List the active vendor's servers (unfiltered) and flag which Atlas already
	models by provider_resource_id. Read-only — inserts nothing; only
	`import_servers` writes."""
	modeled = set(
		frappe.get_all(
			"Server",
			filters={"provider_type": provider_type},
			pluck="provider_resource_id",
		)
	)
	out: list[dict] = []
	for discovered in providers.for_provider_type(provider_type).list_servers():
		out.append(
			{
				"provider_resource_id": discovered.provider_resource_id,
				"title": discovered.title,
				"ipv4_address": discovered.ipv4_address,
				"size": discovered.size,
				"imported": discovered.provider_resource_id in modeled,
			}
		)
	return out


def import_servers(provider_type: str, resource_ids: list[str]) -> dict:
	"""Adopt already-provisioned vendor servers as `Pending` Server rows.

	For each picked vendor id (skipping any Atlas already models): re-resolve the
	box authoritatively via `describe()` — the same path `finish_provisioning`
	trusts — and write a Server row through `_apply_describe_result`, so import and
	provision agree on how a `ProvisionResult` becomes the networking/size/image
	fields. The human `title` comes from the vendor hostname (sourced from the same
	`list_servers()` discovery the picker used), falling back to the resource id —
	`describe()` doesn't surface a clean hostname, and `title` is an editable label,
	not an immutable networking field. The row lands `Pending`: its origin is unknown
	(hand-built or an old Atlas box) and Atlas has not bootstrapped it, so the
	operator drives it to Active with Bootstrap / Re-bootstrap. Returns the names +
	titles imported and the ids skipped as already-modeled (belt-and-braces dedup;
	`discover_servers` already dims them)."""
	from atlas.atlas.providers.worker import _apply_describe_result

	provider_impl = providers.for_provider_type(provider_type)
	modeled = set(
		frappe.get_all(
			"Server",
			filters={"provider_type": provider_type},
			pluck="provider_resource_id",
		)
	)
	# Map vendor id → hostname from the same discovery source the picker rendered, so
	# the imported row is titled with the friendly name the operator ticked, not a
	# UUID. One list call regardless of how many ids were picked.
	hostnames = {server.provider_resource_id: server.title for server in provider_impl.list_servers()}
	imported: list[dict] = []
	skipped: list[str] = []
	for resource_id in resource_ids:
		if resource_id in modeled:
			skipped.append(resource_id)
			continue
		result = provider_impl.describe(resource_id)
		preferred_title = hostnames.get(resource_id) or result.provider_resource_id or resource_id
		server = frappe.get_doc(
			{
				"doctype": "Server",
				"title": _unique_server_title(preferred_title),
				"provider_type": provider_type,
				"provider_resource_id": result.provider_resource_id or resource_id,
				"status": "Pending",
			}
		)
		_apply_describe_result(server, result)
		server.insert(ignore_permissions=True)
		modeled.add(resource_id)
		imported.append({"name": server.name, "title": server.title})
	return {"imported": imported, "skipped": skipped}


def _unique_server_title(preferred: str) -> str:
	"""A Server.title that doesn't collide with an existing row. Discovery's
	preferred title is the vendor hostname (or the resource id when unnamed); a
	box adopted under a name another Server already uses gets a `-2`, `-3`, …
	suffix so the insert doesn't trip the unique-title guard `provision_server`
	enforces. Pure string work — no vendor call."""
	if not frappe.db.exists("Server", {"title": preferred}):
		return preferred
	suffix = 2
	while frappe.db.exists("Server", {"title": f"{preferred}-{suffix}"}):
		suffix += 1
	return f"{preferred}-{suffix}"


def discover_reserved_ips(provider_type: str) -> list[dict]:
	"""List the active vendor's reserved IPs (fleet-wide) and, for each, resolve the
	Server it maps to by matching `droplet_resource_id` against Server
	`provider_resource_id`. Read-only — inserts nothing; only `import_reserved_ips`
	writes.

	The per-Server `Reserved IP.discover(server)` needs you to name the Server first;
	this is the recovery path after the Reserved IP rows are gone (e.g. a server
	reset dropped them) — the vendor still holds the IPs and each one's droplet
	binding tells us which Server it belongs to. A floating IP (bound to no droplet)
	resolves to no Server: importable, resting unattached until reassigned."""
	modeled = set(
		frappe.get_all("Reserved IP", pluck="ip_address"),
	)
	# vendor droplet id → Server name, so a discovered IP's droplet_resource_id maps
	# straight back to the Server row. One query regardless of fleet size.
	droplet_to_server = {
		row.provider_resource_id: row.name
		for row in frappe.get_all(
			"Server",
			filters={"provider_type": provider_type},
			fields=["name", "provider_resource_id"],
		)
		if row.provider_resource_id
	}
	out: list[dict] = []
	for reserved in providers.for_provider_type(provider_type).list_reserved_ips():
		out.append(
			{
				"ip_address": reserved.ip_address,
				"provider_resource_id": reserved.provider_resource_id,
				"droplet_resource_id": reserved.droplet_resource_id,
				"server": droplet_to_server.get(reserved.droplet_resource_id),
				"imported": reserved.ip_address in modeled,
			}
		)
	return out


def import_reserved_ips(provider_type: str, ip_addresses: list[str]) -> dict:
	"""Adopt the picked vendor reserved IPs as `Reserved IP` rows, auto-mapping each
	to its Server by droplet binding. Idempotent: an already-modeled address is
	skipped, never double-inserted.

	A floating IP (no droplet, or a droplet Atlas doesn't model) imports with `server`
	unset — `Reserved IP.server` is not immutable and can rest with no Server, so the
	operator reassign()s it later. Returns the addresses imported and those skipped as
	already-modeled (belt-and-braces dedup; `discover_reserved_ips` already dims
	them)."""
	provider_impl = providers.for_provider_type(provider_type)
	modeled = set(frappe.get_all("Reserved IP", pluck="ip_address"))
	droplet_to_server = {
		row.provider_resource_id: row.name
		for row in frappe.get_all(
			"Server",
			filters={"provider_type": provider_type},
			fields=["name", "provider_resource_id"],
		)
		if row.provider_resource_id
	}
	# vendor address → payload from the same discovery source the picker rendered, so
	# import re-resolves the droplet binding authoritatively (one list call).
	by_address = {reserved.ip_address: reserved for reserved in provider_impl.list_reserved_ips()}
	imported: list[dict] = []
	skipped: list[str] = []
	for ip_address in ip_addresses:
		if ip_address in modeled:
			skipped.append(ip_address)
			continue
		reserved = by_address.get(ip_address)
		if not reserved:
			# The IP vanished from the vendor between discover and import — skip it
			# rather than write a row for an address the vendor no longer holds.
			skipped.append(ip_address)
			continue
		row = frappe.get_doc(
			{
				"doctype": "Reserved IP",
				"ip_address": reserved.ip_address,
				"provider_resource_id": reserved.provider_resource_id,
				"server": droplet_to_server.get(reserved.droplet_resource_id),
			}
		)
		row.insert(ignore_permissions=True)
		modeled.add(ip_address)
		imported.append({"name": row.name, "ip_address": row.ip_address, "server": row.server})
	return {"imported": imported, "skipped": skipped}


def upsert_catalog(provider_type: str, capabilities) -> dict:
	"""Upsert Provider Size / Provider Image rows from a Capabilities dataclass.

	Returns counts of inserted / updated / disabled rows.
	"""
	inserted = updated = disabled = 0
	seen_size_names: set[str] = set()
	seen_image_names: set[str] = set()

	# A discover() hint only takes effect when no row of this provider_type is
	# already marked default — an operator/config choice (set after discover) and a
	# later manual flip always win. Resolve "already has a default" once, up front,
	# so re-discovering never clobbers an existing default.
	size_default_taken = bool(
		frappe.db.exists("Provider Size", {"provider_type": provider_type, "is_default": 1})
	)
	image_default_taken = bool(
		frappe.db.exists("Provider Image", {"provider_type": provider_type, "is_default": 1})
	)
	hinted_default_size = next((s.slug for s in capabilities.sizes if s.is_default), None)
	hinted_default_image = next((i.slug for i in capabilities.images if i.is_default), None)

	for size in capabilities.sizes:
		size_name = f"{provider_type}/{size.slug}"
		seen_size_names.add(size_name)
		metadata_json = json.dumps(size.provider_metadata or {})
		if frappe.db.exists("Provider Size", size_name):
			frappe.db.set_value(
				"Provider Size",
				size_name,
				{
					"enabled": 1,
					"monthly_cost_usd": size.monthly_cost_usd,
					"provider_metadata": metadata_json,
				},
			)
			updated += 1
		else:
			frappe.get_doc(
				{
					"doctype": "Provider Size",
					"provider_type": provider_type,
					"slug": size.slug,
					"enabled": 1,
					"monthly_cost_usd": size.monthly_cost_usd,
					"provider_metadata": metadata_json,
				}
			).insert(ignore_permissions=True)
			inserted += 1

	for image in capabilities.images:
		image_name = f"{provider_type}/{image.slug}"
		seen_image_names.add(image_name)
		metadata_json = json.dumps(image.provider_metadata or {})
		if frappe.db.exists("Provider Image", image_name):
			frappe.db.set_value(
				"Provider Image",
				image_name,
				{"enabled": 1, "provider_metadata": metadata_json},
			)
			updated += 1
		else:
			frappe.get_doc(
				{
					"doctype": "Provider Image",
					"provider_type": provider_type,
					"slug": image.slug,
					"enabled": 1,
					"provider_metadata": metadata_json,
				}
			).insert(ignore_permissions=True)
			inserted += 1

	existing_sizes = frappe.get_all(
		"Provider Size",
		filters={"provider_type": provider_type, "enabled": 1},
		pluck="name",
	)
	for name in existing_sizes:
		if name not in seen_size_names:
			frappe.db.set_value("Provider Size", name, "enabled", 0)
			disabled += 1

	existing_images = frappe.get_all(
		"Provider Image",
		filters={"provider_type": provider_type, "enabled": 1},
		pluck="name",
	)
	for name in existing_images:
		if name not in seen_image_names:
			frappe.db.set_value("Provider Image", name, "enabled", 0)
			disabled += 1

	# Adopt the provider's default hint only into an empty slot (nothing already
	# default). set_default saves through the row controller, enforcing one default.
	from atlas.atlas.setup_catalog import set_default

	if hinted_default_size and not size_default_taken:
		set_default("Provider Size", provider_type, hinted_default_size)
	if hinted_default_image and not image_default_taken:
		set_default("Provider Image", provider_type, hinted_default_image)

	return {"inserted": inserted, "updated": updated, "disabled": disabled}
