"""E2E configuration readers and stable test artifacts.

These pull from `frappe.conf` (site config) and from `~/.cache/atlas-e2e/`,
so that every phase shares the same inputs without each one re-deriving them.
"""

import os
import subprocess

import frappe

from atlas.atlas.digitalocean import DigitalOceanClient
from atlas.atlas.ssh import Connection

TAG = "atlas-e2e"
SWEEP_AGE_SECONDS = 30 * 60

# Public Firecracker CI Ubuntu 24.04 artifacts (pinned for stability).
# image_name bumped to -v2 when the baked-in guest unit changed (NAT44 v4
# egress). Images are immutable (see Virtual Machine Image controller): a
# changed rootfs — and the guest atlas-network.service is part of the rootfs —
# is a NEW image, never a rewrite. A fresh image_name has no rootfs on any
# server, so sync-image.sh rebuilds instead of short-circuiting on the stale
# ext4, and the new guest unit lands.
DEFAULT_IMAGE = {
	"image_name": "ubuntu-24.04-v2",
	"title": "Firecracker CI Ubuntu 24.04 rootfs (NAT44 v4 egress)",
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


def get_phase1_connection() -> Connection:
	host = frappe.conf.get("atlas_phase1_host")
	key = frappe.conf.get("atlas_phase1_ssh_private_key")
	if not host or not key:
		raise MissingConfig(
			"Phase 1 e2e requires atlas_phase1_host and atlas_phase1_ssh_private_key in site config."
		)
	return Connection(host=host, ssh_private_key=_load_key(key))


def get_client() -> DigitalOceanClient:
	token = frappe.conf.get("atlas_do_token")
	if not token:
		raise MissingConfig(
			"e2e needs atlas_do_token in site config: "
			"bench --site <site> set-config -p atlas_do_token <DO_TOKEN>"
		)
	return DigitalOceanClient(token=token)


def get_ssh_key_id() -> str:
	"""Read the SSH fingerprint for e2e from site config.

	Site config is the source of truth for the e2e harness — Atlas Settings
	gets written *to* during `ensure_e2e_provider`, so reading from it would
	pick up stale values left by prior runs (e.g. unit-test fixtures'
	`fp:fingerprint`)."""
	key_id = frappe.conf.get("atlas_ssh_key_id")
	if not key_id:
		raise MissingConfig("e2e needs atlas_ssh_key_id in site config")
	return key_id


def get_ssh_private_key_path() -> str:
	"""Absolute path on disk to the SSH private key for e2e.

	Reads from site config (`atlas_ssh_private_key_path` direct, or
	`atlas_ssh_private_key` inline-PEM that we spill to a cache file).
	Site config wins over Atlas Settings for the same reason as
	`get_ssh_key_id` — the Single is a write target during e2e setup."""
	path = frappe.conf.get("atlas_ssh_private_key_path")
	if path:
		expanded = os.path.expanduser(path)
		if not os.path.isfile(expanded):
			raise MissingConfig(f"atlas_ssh_private_key_path {path!r} is not a file")
		return expanded
	pem = frappe.conf.get("atlas_ssh_private_key")
	if not pem:
		raise MissingConfig(
			"e2e needs atlas_ssh_private_key_path or atlas_ssh_private_key in site config."
		)
	cache_dir = os.path.expanduser("~/.cache/atlas-e2e")
	os.makedirs(cache_dir, exist_ok=True)
	spilled = os.path.join(cache_dir, "provider-key.pem")
	with open(spilled, "w") as handle:
		handle.write(_load_key(pem))
	os.chmod(spilled, 0o600)
	return spilled


def get_region() -> str:
	return frappe.conf.get("atlas_test_region", "blr1")


def get_size() -> str:
	return frappe.conf.get("atlas_test_size", "s-2vcpu-4gb-intel")


def get_image() -> str:
	return frappe.conf.get("atlas_test_image", "ubuntu-24-04-x64")


def _ephemeral_key_path() -> str:
	"""Stable ed25519 keypair under `~/.cache/atlas-e2e/`; generated once."""
	directory = os.path.expanduser("~/.cache/atlas-e2e")
	os.makedirs(directory, exist_ok=True)
	key_path = os.path.join(directory, "id")
	if not os.path.exists(key_path):
		subprocess.run(
			["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
			check=True,
		)
	os.chmod(key_path, 0o600)
	return key_path


def ephemeral_public_key() -> str:
	"""Return the public half of the stable ed25519 keypair. Returning the same
	`.pub` lets phases 5 and 6 inject an SSH key the operator can point at the
	same authorized_keys entry across runs."""
	with open(f"{_ephemeral_key_path()}.pub") as handle:
		return handle.read().strip()


def ephemeral_private_key() -> str:
	"""Return the private half of the stable ed25519 keypair, for probes that
	need to SSH back into a freshly provisioned VM."""
	with open(_ephemeral_key_path()) as handle:
		return handle.read()
