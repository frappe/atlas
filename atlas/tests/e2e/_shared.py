"""Shared e2e helpers."""

import os
import time
from datetime import datetime, timezone

import frappe

from atlas.atlas.digitalocean import DigitalOceanClient

TAG = "atlas-e2e"
SWEEP_AGE_SECONDS = 30 * 60

# Public Firecracker CI Ubuntu 24.04 artifacts (pinned for stability).
DEFAULT_IMAGE = {
	"image_name": "ubuntu-24.04",
	"description": "Firecracker CI Ubuntu 24.04 rootfs",
	"kernel_url": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128",
	"kernel_filename": "vmlinux-6.1.128",
	"kernel_sha256": "27a8310b9a727517e9eb02044524b6ceb77de5728e3491b6974d5c846227ecc8",
	"rootfs_url": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/ubuntu-24.04.squashfs",
	"rootfs_filename": "ubuntu-24.04.ext4",
	"rootfs_sha256": "88821a26b5a38c92b84a064d452167d7f80f9e17cf4441d1ebbae7569e340aee",
	"default_disk_gigabytes": 4,
}


class MissingConfig(Exception):
	pass


def _load_key(value: str) -> str:
	"""Accept either inline PEM contents or a path to a key file.

	A value that looks like a path (no PEM header, starts with `~` or `/`)
	is expanded and read from disk.
	"""
	if value.lstrip().startswith("-----BEGIN"):
		return value
	path = os.path.expanduser(value)
	if not os.path.isfile(path):
		raise MissingConfig(f"ssh private key not found at {path!r}")
	with open(path) as handle:
		return handle.read()


def get_phase1_connection() -> dict:
	host = frappe.conf.get("atlas_phase1_host")
	key = frappe.conf.get("atlas_phase1_ssh_private_key")
	if not host or not key:
		raise MissingConfig(
			"Phase 1 e2e requires atlas_phase1_host and atlas_phase1_ssh_private_key in site config."
		)
	return {"host": host, "ssh_private_key": _load_key(key), "user": "root"}


def get_client() -> DigitalOceanClient:
	token = frappe.conf.get("atlas_do_token")
	if not token:
		raise MissingConfig(
			"e2e needs atlas_do_token in site config: "
			"bench --site <site> set-config -p atlas_do_token <DO_TOKEN>"
		)
	return DigitalOceanClient(token=token)


def get_ssh_key_id() -> str:
	key_id = frappe.conf.get("atlas_ssh_key_id")
	if not key_id:
		raise MissingConfig("e2e needs atlas_ssh_key_id in site config")
	return key_id


def get_ssh_private_key() -> str:
	key = frappe.conf.get("atlas_ssh_private_key")
	if not key:
		raise MissingConfig("e2e needs atlas_ssh_private_key in site config")
	return _load_key(key)


def get_region() -> str:
	return frappe.conf.get("atlas_test_region", "blr1")


def get_size() -> str:
	return frappe.conf.get("atlas_test_size", "s-2vcpu-4gb-intel")


def get_image() -> str:
	return frappe.conf.get("atlas_test_image", "ubuntu-24-04-x64")


def sweep_old_droplets(client: DigitalOceanClient) -> None:
	"""List (never delete) droplets tagged `atlas-e2e` older than SWEEP_AGE_SECONDS.

	This DO account also hosts production droplets, so we never auto-delete
	by tag. The operator reviews this list and deletes leaked droplets by
	hand. Per-run cleanup (delete-by-ID, only droplets created in this run)
	is still done in the per-phase `finally`.
	"""
	now = datetime.now(timezone.utc)
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


def wait_for_task(
	task_name: str,
	timeout_seconds: int,
	poll_seconds: float = 1.0,
) -> "frappe.model.document.Document":
	"""Poll a Task row to Success or Failure, or AssertionError on timeout.

	Also raises if the row sits in Running well past its own declared timeout,
	which means the worker died between "set Running" and the final update.
	"""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		task = frappe.get_doc("Task", task_name)
		if task.status in ("Success", "Failure"):
			return task
		if task.status == "Running" and task.started:
			age = (frappe.utils.now_datetime() - task.started).total_seconds()
			if age > 2 * timeout_seconds:
				raise AssertionError(
					f"task {task_name} is orphaned (Running for {age:.0f}s, "
					f"declared timeout {timeout_seconds}s)"
				)
		time.sleep(poll_seconds)
	raise AssertionError(f"task {task_name} did not finish within {timeout_seconds}s")


def server_is_reachable(server_name: str, timeout_seconds: int = 5) -> bool:
	"""Quick SSH liveness probe. Does NOT update Server.status — that's a
	separate decision the caller makes, because Active→Broken is a real state
	change with downstream consequences.
	"""
	from atlas.atlas.ssh import connection_for_server, wait_for_ssh  # noqa: PLC0415

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
	from atlas.atlas.server_provider import provision_server  # noqa: PLC0415

	provider = _ensure_e2e_provider()
	server_name = f"atlas-e2e-shared-{int(time.time())}"
	provision_server(provider, server_name)

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


def _ensure_e2e_provider() -> "frappe.model.document.Document":
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


def ensure_image_on_server(server_name: str) -> "frappe.model.document.Document":
	"""Sync DEFAULT_IMAGE to `server_name` if not already present.

	Probes the server first; if the rootfs is already there, returns the
	Virtual Machine Image doc without re-syncing. Otherwise kicks off
	`sync_to_server` and waits on the Task.
	"""
	from atlas.atlas.ssh import run_task_on_server  # noqa: PLC0415

	image_name = DEFAULT_IMAGE["image_name"]
	if frappe.db.exists("Virtual Machine Image", image_name):
		image = frappe.get_doc("Virtual Machine Image", image_name)
		image.update(DEFAULT_IMAGE)
		image.is_active = 1
		image.save(ignore_permissions=True)
		frappe.db.commit()
	else:
		image = frappe.get_doc({
			"doctype": "Virtual Machine Image",
			**DEFAULT_IMAGE,
			"is_active": 1,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	# Cheap remote probe — if the rootfs is already on disk, skip sync.
	try:
		run_task_on_server(
			server=server_name,
			script="probe-image-present.sh",
			variables={
				"IMAGE_NAME": image.image_name,
				"ROOTFS_FILENAME": image.rootfs_filename,
			},
			timeout_seconds=30,
		)
		return image
	except frappe.ValidationError:
		pass  # not present; fall through to sync

	task_name = image.sync_to_server(server_name)
	task = wait_for_task(task_name, timeout_seconds=900, poll_seconds=5)
	if task.status != "Success":
		raise AssertionError(f"sync-image failed: {(task.stderr or '')[:500]}")
	return image


def mark_orphan_tasks_failure(older_than_minutes: int = 10) -> int:
	"""Mark Running Tasks older than N minutes as Failure. Safety net for
	workers that died mid-job. Returns count marked.
	"""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-older_than_minutes)
	stuck = frappe.get_all(
		"Task",
		filters={"status": "Running", "started": ["<", cutoff]},
		pluck="name",
	)
	for name in stuck:
		doc = frappe.get_doc("Task", name)
		doc.status = "Failure"
		doc.stderr = (doc.stderr or "") + (
			f"\n[atlas e2e] marked Failure: Running for >{older_than_minutes} min "
			"(worker presumed dead)\n"
		)
		doc.ended = frappe.utils.now_datetime()
		doc.save(ignore_permissions=True)
	frappe.db.commit()
	if stuck:
		print(f"[e2e] marked {len(stuck)} orphan Task(s) as Failure")
	return len(stuck)


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
