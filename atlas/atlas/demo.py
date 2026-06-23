"""Populate a dev site with a realistic, varied fleet — no real cloud resources.

Run:

    bench --site <site> execute atlas.atlas.demo.run
    bench --site <site> execute atlas.atlas.demo.run --kwargs "{'reset': True}"

Everything is built on the **Fake provider** (see `providers/fake.py`), so the
rows are produced by the *real* controllers — servers march Pending → Active
through the real worker, VMs walk the real lifecycle, Tasks accumulate honestly —
but nothing touches SSH or a vendor API. That makes the data internally
consistent (derived fields, statuses, networking all real) and makes this script
double as a smoke test of the fake seam.

`developer_mode`-gated. Idempotent: `reset=True` wipes everything on the Fake
providers first (cascading their servers/VMs/snapshots/tasks/IPs), then rebuilds;
a plain run reuses what's already there.

The dataset spans every status and most feature flags so the desk and the
dashboard SPA have something lived-in to render. The data tables and the
per-DocType builders live in `demo_data.py`.
"""

from __future__ import annotations

import frappe

from atlas.atlas import demo_data
from atlas.atlas.providers.fake import FAKE_PROVIDER_TYPE, require_developer_mode

# The active compute vendor for the demo. Servers march Pending → Active through
# the real (faked) worker. The demo also stands up a Self-Managed host directly —
# a Server carries its own provider_type, so the fleet shows two vendor types even
# though only one is "active".
ACTIVE_PROVIDER_TYPE = FAKE_PROVIDER_TYPE


def run(reset: bool = False) -> None:
	"""Build (or rebuild) the demo fleet. Returns nothing; prints a summary."""
	require_developer_mode()
	if reset:
		wipe()
	_ensure_settings()
	_ensure_catalog()
	images = _ensure_images()
	servers = _ensure_servers()
	machines = _ensure_virtual_machines(servers, images)
	_ensure_snapshots(machines)
	_ensure_reserved_ips(servers, machines)
	demo_data.backdate_tasks(servers, machines)
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the full seeded demo dataset before printing the summary
	frappe.db.commit()
	_print_summary(servers, machines)


def wipe() -> None:
	"""Delete every Server the demo created, plus the demo images.

	A real operator's hosts are never touched: the demo identifies its own rows by
	provider_type (Fake) plus the demo's own Self-Managed host (by its well-known
	title). Order matters: dependents before their parents."""
	demo_servers = _demo_server_names()
	_delete_children_of_servers(demo_servers)
	for server in demo_servers:
		frappe.delete_doc("Server", server, force=True, ignore_permissions=True)
	demo_data.delete_demo_images()
	# Leave the catalog + Atlas Settings provider_type in place — cheap to reuse,
	# and the active-vendor pointer stays valid across a reset.
	# nosemgrep: frappe-manual-commit -- demo teardown: commit after wipe so the demo-row deletions are durable
	frappe.db.commit()


def _demo_server_names() -> list[str]:
	"""Every Server the demo created: the Fake-provisioned fleet, plus the demo's
	own Self-Managed host (identified by its well-known title)."""
	servers = frappe.get_all("Server", filters={"provider_type": FAKE_PROVIDER_TYPE}, pluck="name")
	metal = frappe.db.get_value("Server", {"title": demo_data.SELF_MANAGED_SERVER["title"]}, "name")
	if metal:
		servers.append(metal)
	return servers


def _delete_children_of_servers(servers: list[str]) -> None:
	if not servers:
		return
	# A Reserved IP that's still attached refuses deletion (its on_trash guards
	# the invariant). Detach first, then delete. The host-side detach Task is
	# faked (these are Fake servers), so it's a no-op DB update.
	for name in frappe.get_all("Reserved IP", filters={"server": ["in", servers]}, pluck="name"):
		ip = frappe.get_doc("Reserved IP", name)
		if ip.virtual_machine:
			ip.detach()
	for doctype in ("Reserved IP", "Task", "Virtual Machine Snapshot", "Virtual Machine"):
		for name in frappe.get_all(doctype, filters={"server": ["in", servers]}, pluck="name"):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)


def _ensure_settings() -> None:
	"""Point Atlas Settings at the Fake provider and set throwaway dev SSH values
	+ a default user image + a little oversubscription so the capacity math shows."""
	frappe.db.set_single_value("Atlas Settings", "provider_type", ACTIVE_PROVIDER_TYPE, update_modified=False)
	frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1.5, update_modified=False)
	frappe.db.set_single_value(
		"Atlas Settings", "default_user_image", demo_data.DEFAULT_USER_IMAGE, update_modified=False
	)
	# A Fake server never SSHes, so this key is never read on the happy path. But
	# `connection_for_server` reads it eagerly, so any path that bypasses the fake
	# guard (e.g. a stale worker holding pre-guard code) would throw a confusing
	# "key not found" instead of failing cleanly. Write a real throwaway key so the
	# path always resolves — defense-in-depth, the guard stays the real protection.
	frappe.db.set_single_value(
		"Atlas Settings", "ssh_private_key_path", _ensure_throwaway_ssh_key(), update_modified=False
	)
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the seeded Atlas Settings so later seed phases see the active provider and SSH key
	frappe.db.commit()


def _ensure_throwaway_ssh_key() -> str:
	"""Write a 0600 dummy private key under the site's private files and return its
	absolute path. Idempotent; the bytes are never used to connect to anything."""
	import os

	path = frappe.get_site_path("private", "files", "atlas-demo-key.pem")
	if not os.path.exists(path):
		os.makedirs(os.path.dirname(path), exist_ok=True)
		# nosemgrep: frappe-security-file-traversal -- fixed path under the site's private files dir (frappe.get_site_path), not untrusted web input
		with open(path, "w") as handle:
			handle.write(
				"-----BEGIN OPENSSH PRIVATE KEY-----\nfake-demo-key-never-used\n-----END OPENSSH PRIVATE KEY-----\n"
			)
		os.chmod(path, 0o600)
	return os.path.abspath(path)


def _ensure_catalog() -> None:
	"""Seed Provider Size / Provider Image for Fake via the real Refresh-Catalog
	path, so every Server/size Link resolves."""
	frappe.get_single("Atlas Settings").refresh_catalog()
	# nosemgrep: frappe-manual-commit -- demo seeder: persist the seeded catalog so later seed phases' size and image Links resolve
	frappe.db.commit()


def _ensure_images() -> dict[str, str]:
	"""Insert the demo VM images (one archived). Returns {key: image_name}."""
	return demo_data.ensure_images()


def _ensure_servers() -> dict[str, str]:
	"""Stand up the demo servers across the status spectrum. Returns {key: name}.

	Active servers go through the real provision → worker path (faked); the
	off-nominal states (Bootstrapping / Broken / Draining / Self-Managed) are set
	deliberately afterwards because the happy path can't land on them."""
	return demo_data.ensure_servers(ACTIVE_PROVIDER_TYPE)


def _ensure_virtual_machines(servers: dict[str, str], images: dict[str, str]) -> dict[str, str]:
	return demo_data.ensure_virtual_machines(servers, images)


def _ensure_snapshots(machines: dict[str, str]) -> None:
	demo_data.ensure_snapshots(machines)


def _ensure_reserved_ips(servers: dict[str, str], machines: dict[str, str]) -> None:
	demo_data.ensure_reserved_ips(servers, machines)


def _print_summary(servers: dict[str, str], machines: dict[str, str]) -> None:
	counts = {
		doctype: frappe.db.count(doctype)
		for doctype in (
			"Server",
			"Virtual Machine",
			"Virtual Machine Image",
			"Virtual Machine Snapshot",
			"Reserved IP",
			"Task",
		)
	}
	print("[demo] populated the Fake fleet:")
	for doctype, count in counts.items():
		print(f"[demo]   {doctype}: {count}")
	print(
		f"[demo] active provider_type = {ACTIVE_PROVIDER_TYPE}; "
		f"{len(servers)} servers, {len(machines)} VMs seeded"
	)
