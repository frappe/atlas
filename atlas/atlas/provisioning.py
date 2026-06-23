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

	Sourced from `frappe.conf` (`atlas_tls_region`, falling back to
	`atlas_do_region`) — the same per-bench config `bootstrap.py` seeds the active
	Root Domain from, and available from the very first bootstrap step (before the
	Root Domain row exists). Falls back to the active Root Domain's region once
	one is configured, then to `"x"` so an unconfigured dev bench still provisions.
	"""
	region = frappe.conf.get("atlas_tls_region") or frappe.conf.get("atlas_do_region")
	if region:
		return region
	from atlas.atlas.placement import active_root_domain

	try:
		return active_root_domain().region
	except frappe.ValidationError:
		return "x"


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


def _provider_settings(provider_type: str):
	"""The per-vendor Settings Single for a provider type, or None when the
	provider has none (the Fake provider). Vendors with an API always have one."""
	if not frappe.db.exists("DocType", f"{provider_type} Settings"):
		return None
	return frappe.get_single(f"{provider_type} Settings")


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
		# Vendors with an API read their per-vendor Settings Single for the
		# default size/image. The Fake provider (developer_mode) has no Settings
		# Single — it defaults size/image inside provision() — so fall back to the
		# dialog values when no Settings DocType exists. DO/Scaleway are unchanged
		# (their Single exists, so the defaults still apply).
		settings = _provider_settings(provider_type)
		size = dialog_fields.get("size") or (settings.default_size if settings else "")
		image = dialog_fields.get("image") or (settings.default_image if settings else "")
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


def upsert_catalog(provider_type: str, capabilities) -> dict:
	"""Upsert Provider Size / Provider Image rows from a Capabilities dataclass.

	Returns counts of inserted / updated / disabled rows.
	"""
	inserted = updated = disabled = 0
	seen_size_names: set[str] = set()
	seen_image_names: set[str] = set()

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

	return {"inserted": inserted, "updated": updated, "disabled": disabled}
