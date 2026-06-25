"""Scaleway provider seed + lifecycle helpers for the e2e harness.

The DO harness lives in `_droplets.py`; this is its Scaleway twin, kept
separate because Scaleway is bare metal with a different auth/seed shape:

- The SSH key is uploaded at provision time (IAM), not pre-registered out of
  band like DO. `ensure_scaleway_provider` registers the control-plane public
  key with IAM once and caches the returned UUID on `Scaleway Settings.ssh_key_id`
  (the `SshKey.vendor_id` the provider installs). Re-runs reuse it.
- `discover()` hits the live catalog (the only source of the per-zone
  `offer_id` / `os_id` UUIDs), so the seed runs `Atlas Settings.refresh_catalog`
  rather than reading hand-maintained constants.

`sweep_old_scaleway_servers` mirrors the DO sweep: it LISTS (never auto-deletes)
tagged Elastic Metal servers, because the account is shared. Per-run teardown is
the use case's `finally`.
"""

import subprocess
import time

import frappe

from atlas.atlas.scaleway import ScalewayClient
from atlas.tests.e2e._config import TAG, get_scaleway_config, get_ssh_private_key_path

# The control-plane IAM SSH-key name prefix (the Provider DocType is gone; the
# active vendor is `Atlas Settings.provider_type`).
PROVIDER_NAME = "atlas-e2e-scaleway"


def scaleway_client() -> ScalewayClient:
	config = get_scaleway_config()
	return ScalewayClient(secret_key=config["secret_key"], zone=config["zone"])


def _control_plane_public_key() -> str:
	"""The public half of the control-plane SSH key (the one
	`connection_for_server` uses to reach the server as root). Scaleway installs
	this key body via IAM so the bootstrap SSH lands."""
	private_path = get_ssh_private_key_path()
	result = subprocess.run(
		["ssh-keygen", "-y", "-f", private_path],
		check=True,
		capture_output=True,
		text=True,
	)
	return result.stdout.strip()


def _ensure_iam_ssh_key(client: ScalewayClient, project_id: str, public_key: str) -> str:
	"""Return the IAM SSH-key UUID for the control-plane key, registering it once.

	Matches on the public-key body (the base64 blob, ignoring the comment) so a
	re-run reuses the existing IAM key instead of piling up duplicates."""
	wanted_blob = _key_blob(public_key)
	for key in client.list_ssh_keys(project_id):
		if _key_blob(key.get("public_key", "")) == wanted_blob:
			return key["id"]
	created = client.register_ssh_key(
		name=f"{PROVIDER_NAME}-{int(time.time())}",
		public_key=public_key,
		project_id=project_id,
	)
	return created["id"]


def _key_blob(public_key: str) -> str:
	"""The middle (base64) field of an OpenSSH public key — identifies the key
	independent of its trailing comment."""
	parts = public_key.split()
	return parts[1] if len(parts) >= 2 else public_key


def ensure_scaleway_provider() -> str:
	"""Seed the Scaleway e2e Settings + catalog + Atlas Settings.

	Idempotent. Registers the control-plane key with IAM (caching the UUID),
	seeds `Scaleway Settings`, points `Atlas Settings.provider_type` at Scaleway,
	runs `Atlas Settings.refresh_catalog` to populate Provider Size / Provider
	Image rows with the live per-zone offer_id / os_id, and verifies the
	configured default size/image rows exist. Returns the provider_type string
	`"Scaleway"`.
	"""
	import frappe.utils.password

	config = get_scaleway_config()
	client = scaleway_client()

	# 1. Scaleway Settings (secret via the password store, like DO's api_token).
	frappe.db.set_single_value("Scaleway Settings", "project_id", config["project_id"], update_modified=False)
	if config["organization_id"]:
		frappe.db.set_single_value(
			"Scaleway Settings", "organization_id", config["organization_id"], update_modified=False
		)
	frappe.db.set_single_value("Scaleway Settings", "zone", config["zone"], update_modified=False)
	frappe.db.set_single_value("Scaleway Settings", "billing", config["billing"], update_modified=False)
	frappe.utils.password.set_encrypted_password(
		"Scaleway Settings", "Scaleway Settings", config["secret_key"], "secret_key"
	)

	# 2. IAM key — register the control-plane key, cache the UUID on Scaleway Settings.
	public_key = _control_plane_public_key()
	key_id = _ensure_iam_ssh_key(client, config["project_id"], public_key)
	frappe.db.set_single_value("Atlas Settings", "provider_type", "Scaleway", update_modified=False)
	frappe.db.set_single_value("Scaleway Settings", "ssh_key_id", key_id, update_modified=False)
	frappe.db.set_single_value("Atlas Settings", "ssh_public_key", public_key, update_modified=False)
	frappe.db.set_single_value(
		"Atlas Settings", "ssh_private_key_path", get_ssh_private_key_path(), update_modified=False
	)
	frappe.db.commit()

	# 3. Catalog — discover live offers/OS (the only source of the UUIDs).
	frappe.get_single("Atlas Settings").refresh_catalog()
	frappe.db.commit()

	# 4. Settings defaults point at the chosen size/image rows.
	size_name = f"Scaleway/{config['size']}"
	image_name = f"Scaleway/{config['image']}"
	if not frappe.db.exists("Provider Size", size_name):
		raise AssertionError(
			f"Provider Size {size_name!r} not found after discover — check scaleway.size "
			f"against the live catalog (name casing matters, e.g. EM-A610R-NVME)."
		)
	if not frappe.db.exists("Provider Image", image_name):
		raise AssertionError(
			f"Provider Image {image_name!r} not found after discover — check scaleway.image."
		)
	frappe.db.set_single_value("Scaleway Settings", "default_size", size_name, update_modified=False)
	frappe.db.set_single_value("Scaleway Settings", "default_image", image_name, update_modified=False)
	frappe.db.commit()

	print(f"[e2e/scw] Scaleway ready: zone={config['zone']} size={size_name} key_id={key_id}")
	return "Scaleway"


def sweep_old_scaleway_servers() -> None:
	"""LIST (never auto-delete) Elastic Metal servers tagged `atlas-e2e`. The
	account is shared with production, so the operator deletes leaks by hand."""
	client = scaleway_client()
	leaked = client.list_servers_by_tag(TAG)
	if leaked:
		print(f"[e2e/scw] WARNING: {len(leaked)} server(s) tagged {TAG!r} (NOT auto-deleted):")
		for server in leaked:
			print(f"  - id={server.get('id')} name={server.get('name')} status={server.get('status')}")
		print("  Delete manually after verifying none is in production.")


def cleanup_scaleway_server(server_id: str) -> None:
	try:
		scaleway_client().delete_server(server_id)
		print(f"[e2e/scw] deleted server {server_id}")
	except Exception as exception:
		print(f"[e2e/scw] cleanup failed for {server_id}: {exception}")
