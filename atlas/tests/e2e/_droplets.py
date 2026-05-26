"""Droplet lifecycle and shared-server reuse for the e2e harness."""

import time
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime

import frappe

from atlas.atlas.digitalocean import DigitalOceanClient
from atlas.tests.e2e._config import (
	SWEEP_AGE_SECONDS,
	TAG,
	get_client,
	get_image,
	get_region,
	get_size,
	get_ssh_key_id,
	get_ssh_private_key,
)


def sweep_old_droplets(client: DigitalOceanClient) -> None:
	"""List (never delete) droplets tagged `atlas-e2e` older than SWEEP_AGE_SECONDS.

	This DO account also hosts production droplets, so we never auto-delete
	by tag. The operator reviews this list and deletes leaked droplets by
	hand. Per-run cleanup (delete-by-ID, only droplets created in this run)
	is still done in the per-phase `finally`.
	"""
	now = datetime.now(UTC)
	leaked = []
	for droplet in client.list_droplets_by_tag(TAG):
		created_at = droplet.get("created_at")
		if not created_at:
			continue
		try:
			created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
		except ValueError:
			continue
		age = (now - created).total_seconds()
		if age > SWEEP_AGE_SECONDS:
			leaked.append((droplet["id"], droplet["name"], int(age)))
	if leaked:
		print(f"WARNING: {len(leaked)} leaked droplet(s) tagged {TAG!r} (NOT auto-deleted):")
		for droplet_id, name, age_seconds in leaked:
			print(f"  - id={droplet_id} name={name} age={age_seconds}s")
		print("  Delete manually after verifying none is in production.")


def create_test_droplet(client: DigitalOceanClient, name_suffix: str) -> dict:
	"""Create a tagged throwaway droplet and wait for it to be active."""
	name = f"atlas-e2e-{name_suffix}-{int(time.time())}"
	droplet = client.create_droplet(
		name=name,
		region=get_region(),
		size=get_size(),
		image=get_image(),
		ssh_key_ids=[get_ssh_key_id()],
		tags=[TAG, f"phase-{name_suffix}"],
		ipv6=True,
	)
	return client.wait_for_active(droplet["id"], timeout_seconds=300)


def cleanup_droplet(client: DigitalOceanClient, droplet_id: int) -> None:
	try:
		client.delete_droplet(droplet_id)
	except Exception as exception:
		print(f"cleanup failed for {droplet_id}: {exception}")


def server_is_reachable(server_name: str, timeout_seconds: int = 5) -> bool:
	"""Quick SSH liveness probe. Does NOT update Server.status — that's a
	separate decision the caller makes, because Active→Broken is a real state
	change with downstream consequences.
	"""
	from atlas.atlas.ssh import connection_for_server, wait_for_ssh

	server = frappe.get_doc("Server", server_name)
	try:
		wait_for_ssh(
			connection_for_server(server),
			timeout_seconds=timeout_seconds,
			poll_seconds=1,
		)
		return True
	except Exception:
		return False


def ensure_bootstrapped_server(
	reuse: bool = True,
	keep: bool = False,
) -> tuple["frappe.model.document.Document", DigitalOceanClient, bool]:
	"""Return an Active Server with a live droplet.

	If `reuse` and an Active Server is SSH-reachable, return it.
	If a row says Active but SSH is dead, mark it Broken and try the next.
	Otherwise provision a fresh droplet via phase 3's `provision_server`.

	Returns (server_doc, do_client, created_now). `created_now=True` means
	we provisioned in this call. `keep` is accepted so callers can pass
	their flag through; this helper does not perform cleanup itself.
	"""
	_ = keep  # callers gate their own teardown on this; recorded for symmetry
	client = get_client()

	if reuse:
		for name in frappe.get_all(
			"Server", filters={"status": "Active"}, pluck="name"
		):
			if server_is_reachable(name, timeout_seconds=5):
				return frappe.get_doc("Server", name), client, False
			frappe.db.set_value("Server", name, "status", "Broken")
			frappe.db.commit()
			print(f"[e2e] marked {name} Broken (SSH unreachable)")

	# No reusable Active server. Provision fresh via the phase 3 path.
	provider = ensure_e2e_provider()
	server_name = f"atlas-e2e-shared-{int(time.time())}"
	provider.provision_server(server_name)

	deadline = time.monotonic() + 600
	while time.monotonic() < deadline:
		frappe.db.rollback()
		server = frappe.get_doc("Server", server_name)
		if server.status in ("Active", "Broken"):
			break
		time.sleep(5)
	else:
		raise AssertionError(f"server {server_name} did not become Active within 600s")

	if server.status != "Active":
		raise AssertionError(
			f"server {server_name} ended in status {server.status}, expected Active"
		)
	return server, client, True


def ensure_e2e_provider() -> "frappe.model.document.Document":
	name = "atlas-e2e-provider"
	if frappe.db.exists("Server Provider", name):
		return frappe.get_doc("Server Provider", name)
	return frappe.get_doc({
		"doctype": "Server Provider",
		"provider_name": name,
		"provider_type": "DigitalOcean",
		"api_token": frappe.conf.get("atlas_do_token"),
		"ssh_key_id": get_ssh_key_id(),
		"ssh_private_key": get_ssh_private_key(),
		"default_region": get_region(),
		"default_size": get_size(),
		"default_image": get_image(),
		"is_active": 1,
	}).insert(ignore_permissions=True)


@contextmanager
def phase(label: str, reuse: bool = True, keep: bool = True):
	"""Scaffolding for phases 4-7: bootstrap, sweep, time, format, cleanup.

	Wraps the per-phase boilerplate: `ensure_bootstrapped_server`, the leaked-
	droplet pre-sweep, the OK/FAIL one-line summary, the traceback on failure,
	and the per-run droplet cleanup when `keep=False`. Yields the Server doc.
	"""
	start_clock = time.monotonic()
	server, client, created_now = ensure_bootstrapped_server(reuse=reuse, keep=keep)
	sweep_old_droplets(client)
	try:
		yield server
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"{label}: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	else:
		elapsed = time.monotonic() - start_clock
		print(f"{label}: OK in {elapsed:.0f}s")
	finally:
		if created_now and not keep and server.provider_resource_id:
			cleanup_droplet(client, int(server.provider_resource_id))


def teardown_all() -> None:
	"""Print the doctl commands to delete leaked e2e droplets.

	Droplets created via `ensure_bootstrapped_server` go through
	`provision_server`, which tags them `atlas` (shared with production
	droplets), so we can't safely filter the whole `atlas` tag. Instead
	we look at the Server doctype: any row whose name starts with
	`atlas-e2e-` and has a `provider_resource_id` is a candidate to delete.
	Also includes anything tagged `atlas-e2e` from the older per-phase
	create_test_droplet path. Never auto-deletes — the operator copy-pastes
	the printed commands.
	"""
	client = get_client()
	seen: dict[int, str] = {}
	for droplet in client.list_droplets_by_tag(TAG):
		seen[droplet["id"]] = droplet["name"]
	for row in frappe.get_all(
		"Server",
		filters={"server_name": ["like", "atlas-e2e-%"]},
		fields=["name", "provider_resource_id"],
	):
		if row["provider_resource_id"]:
			seen[int(row["provider_resource_id"])] = row["name"]
	if not seen:
		print("[e2e] no e2e droplets found")
		return
	print(f"[e2e] {len(seen)} e2e droplet(s):")
	for droplet_id, name in sorted(seen.items()):
		print(f"  doctl compute droplet delete {droplet_id}  # {name}")
