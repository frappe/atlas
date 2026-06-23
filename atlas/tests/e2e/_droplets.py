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
	get_ssh_private_key_path,
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
	return _wait_for_droplet_active(client, droplet["id"], timeout_seconds=300)


def _wait_for_droplet_active(client: DigitalOceanClient, droplet_id: int, timeout_seconds: int = 300) -> dict:
	"""Replacement for the removed DigitalOceanClient.wait_for_active."""
	deadline = time.monotonic() + timeout_seconds
	while True:
		droplet = client.get_droplet(droplet_id)
		if droplet.get("status") == "active":
			return droplet
		if time.monotonic() >= deadline:
			from atlas.atlas.digitalocean import DigitalOceanError

			raise DigitalOceanError(
				f"Droplet {droplet_id} not active after {timeout_seconds}s (status={droplet.get('status')})"
			)
		time.sleep(5)


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
		active = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")
		print(f"[e2e] reuse: scanning {len(active)} Active Server row(s) for SSH reachability")
		for name in active:
			if server_is_reachable(name, timeout_seconds=5):
				print(f"[e2e] reuse: {name} is reachable, returning it")
				return frappe.get_doc("Server", name), client, False
			frappe.db.set_value("Server", name, "status", "Broken")
			frappe.db.commit()
			print(f"[e2e] marked {name} Broken (SSH unreachable)")

	# No reusable Active server. Provision fresh via the phase 3 path.
	print("[e2e] no reusable Active server, ensuring e2e provider")
	provider_type = ensure_e2e_provider()
	title = f"atlas-e2e-shared-{int(time.time())}"
	print(f"[e2e] provisioning new server {title!r} via provider_type {provider_type!r}")
	server_name = frappe.get_single("Atlas Settings").provision_server(title)
	print(f"[e2e] inserted Server {server_name!r}; enqueued finish_provisioning worker job")

	deadline = time.monotonic() + 600
	last_logged_status = None
	while time.monotonic() < deadline:
		frappe.db.rollback()
		server = frappe.get_doc("Server", server_name)
		if server.status != last_logged_status:
			elapsed = int(time.monotonic() - (deadline - 600))
			print(
				f"[e2e] {server_name!r} status={server.status!r} "
				f"prid={server.provider_resource_id!r} "
				f"ipv4={server.ipv4_address!r} (t+{elapsed}s)"
			)
			last_logged_status = server.status
		if server.status in ("Active", "Broken"):
			break
		time.sleep(5)
	else:
		print(f"[e2e] timeout: dumping recent Tasks for {server_name!r}")
		for task in frappe.get_all(
			"Task",
			filters={"server": server_name},
			fields=["name", "script", "status", "creation"],
			order_by="creation desc",
			limit=5,
		):
			print(f"[e2e]   task {task.name} script={task.script} status={task.status} ({task.creation})")
		raise AssertionError(f"server {title!r} ({server_name}) did not become Active within 600s")

	if server.status != "Active":
		raise AssertionError(
			f"server {title!r} ({server_name}) ended in status {server.status}, expected Active"
		)
	print(f"[e2e] server {server_name!r} is Active in {int(time.monotonic() - (deadline - 600))}s")
	return server, client, True


def ensure_e2e_provider() -> str:
	"""Seed Atlas Settings.provider_type + DigitalOcean Settings + Provider Size /
	Provider Image rows from site config. Returns the active provider_type.
	Idempotent."""
	import frappe.utils.password

	from atlas.tests.fixtures import seed_catalogs

	seed_catalogs()
	provider_type = "DigitalOcean"

	frappe.db.set_single_value("Atlas Settings", "provider_type", provider_type, update_modified=False)
	frappe.db.set_single_value("Atlas Settings", "ssh_key_id", get_ssh_key_id(), update_modified=False)
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_private_key_path",
		get_ssh_private_key_path(),
		update_modified=False,
	)

	# DigitalOcean Settings
	size_name = f"DigitalOcean/{get_size()}"
	image_name = f"DigitalOcean/{get_image()}"
	# Make sure the rows exist (seed_catalogs above seeds the known list,
	# but the operator could be pointing at a custom slug via site config).
	_ensure_catalog_row("Provider Size", size_name, "DigitalOcean", get_size())
	_ensure_catalog_row("Provider Image", image_name, "DigitalOcean", get_image())

	frappe.db.set_single_value("DigitalOcean Settings", "region", get_region(), update_modified=False)
	frappe.db.set_single_value("DigitalOcean Settings", "default_size", size_name, update_modified=False)
	frappe.db.set_single_value("DigitalOcean Settings", "default_image", image_name, update_modified=False)
	token = frappe.conf.get("atlas_do_token")
	if token:
		frappe.utils.password.set_encrypted_password(
			"DigitalOcean Settings", "DigitalOcean Settings", token, "api_token"
		)

	frappe.db.commit()
	return provider_type


def _ensure_catalog_row(doctype: str, name: str, provider_type: str, slug: str) -> None:
	if frappe.db.exists(doctype, name):
		return
	import json

	frappe.get_doc(
		{
			"doctype": doctype,
			"provider_type": provider_type,
			"slug": slug,
			"enabled": 1,
			"provider_metadata": json.dumps({}),
		}
	).insert(ignore_permissions=True)


@contextmanager
def phase(label: str, reuse: bool = True, keep: bool = True):
	"""Scaffolding for use-case modules that need a bootstrapped server.

	Wraps the per-use-case boilerplate: `ensure_bootstrapped_server`, the
	leaked-droplet pre-sweep, the OK/FAIL one-line summary, the traceback on
	failure, and the per-run droplet cleanup when `keep=False`. Yields the
	Server doc.

	The name `phase` is historical — it survives because operators may still
	have `bench execute atlas.tests.e2e._shared.phase` muscle memory. New
	callers can read it as "scope this use case".
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
		filters={"title": ["like", "atlas-e2e-%"]},
		fields=["name", "title", "provider_resource_id"],
	):
		if row["provider_resource_id"]:
			seen[int(row["provider_resource_id"])] = row["title"]
	if not seen:
		print("[e2e] no e2e droplets found")
		return
	print(f"[e2e] {len(seen)} e2e droplet(s):")
	for droplet_id, name in sorted(seen.items()):
		print(f"  doctl compute droplet delete {droplet_id}  # {name}")
