"""E2E configuration readers and stable test artifacts.

These pull from `frappe.conf` (site config) and from `~/.cache/atlas-e2e/`,
so that every phase shares the same inputs without each one re-deriving them.
"""

import os
import subprocess

import frappe

from atlas.atlas.digitalocean import DigitalOceanClient

TAG = "atlas-e2e"
SWEEP_AGE_SECONDS = 30 * 60

# Ubuntu cloud images (noble), pinned to a dated release for stability.
# kernel_sha256 is the digest of the DOWNLOADED packed vmlinuz; sync-image.py
# decompresses the zstd payload to a raw vmlinux on the server. Mirrors
# atlas.bootstrap.DEFAULT_IMAGE / MINIMAL_IMAGE — keep the two in sync.
_NOBLE_RELEASE = "https://cloud-images.ubuntu.com/releases/noble/release-20260518"
_NOBLE_MINIMAL_RELEASE = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260521"

DEFAULT_IMAGE = {
	"image_name": "ubuntu-24.04",
	"title": "Ubuntu 24.04 server cloud image",
	"kernel_url": f"{_NOBLE_RELEASE}/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-server",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_RELEASE}/ubuntu-24.04-server-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-server.ext4",
	"rootfs_sha256": "bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74",
	"default_disk_gigabytes": 4,
}

MINIMAL_IMAGE = {
	"image_name": "ubuntu-24.04-minimal",
	"title": "Ubuntu 24.04 minimal cloud image",
	"kernel_url": f"{_NOBLE_MINIMAL_RELEASE}/unpacked/ubuntu-24.04-minimal-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-minimal",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_MINIMAL_RELEASE}/ubuntu-24.04-minimal-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-minimal.ext4",
	"rootfs_sha256": "a288f0bd499e1a747f86fda8ec9822dd99a4e3c0721d89ffd9dd57608ff21072",
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


def get_client() -> DigitalOceanClient:
	token = frappe.conf.get("atlas_do_token")
	if not token:
		raise MissingConfig(
			"e2e needs atlas_do_token in site config: "
			"bench --site <site> set-config -p atlas_do_token <DO_TOKEN>"
		)
	return DigitalOceanClient(token=token)


def get_ssh_key_id() -> str:
	"""Read the vendor SSH-key id for e2e from site config.

	Site config is the source of truth for the e2e harness — Atlas Settings
	gets written *to* during `ensure_e2e_provider`, so reading from it would
	pick up stale values left by prior runs (e.g. unit-test fixtures'
	`key-id-123`)."""
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
		raise MissingConfig("e2e needs atlas_ssh_private_key_path or atlas_ssh_private_key in site config.")
	cache_dir = os.path.expanduser("~/.cache/atlas-e2e")
	os.makedirs(cache_dir, exist_ok=True)
	spilled = os.path.join(cache_dir, "provider-key.pem")
	with open(spilled, "w") as handle:
		handle.write(_load_key(pem))
	os.chmod(spilled, 0o600)
	return spilled


# Let's Encrypt staging — no rate limits, untrusted cert. The TLS e2e + the
# bootstrap TLS tail default here so a full producer pass (LE → DNS-01 → certbot)
# never burns production issuance quota. Override with atlas_acme_directory_url.
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def get_tls_config() -> dict:
	"""Read the Route 53 + ACME inputs the TLS layer needs from site config.

	Mirrors the DO readers above (site config is the source of truth for the
	harness). Raises `MissingConfig` naming the first absent key, so a site that
	hasn't configured TLS skips the TLS e2e / bootstrap tail cleanly rather than
	failing deep inside certbot.

	Keys:
	    atlas_tls_domain               the wildcard zone, e.g. blr1.frappe.dev
	                                   (its Route 53 hosted zone must exist)
	    atlas_route53_access_key_id    IAM key with route53:* on the zone
	    atlas_route53_secret_access_key  …its secret
	    atlas_acme_account_email       ACME registration / expiry-notice email
	    atlas_acme_directory_url       optional; defaults to LE staging
	"""
	required = (
		"atlas_tls_domain",
		"atlas_route53_access_key_id",
		"atlas_route53_secret_access_key",
		"atlas_acme_account_email",
	)
	values = {}
	for key in required:
		value = frappe.conf.get(key)
		if not value:
			raise MissingConfig(
				f"TLS e2e/bootstrap needs {key!r} in site config: "
				f"bench --site <site> set-config -p {key} <value>"
			)
		values[key] = value
	return {
		"domain": values["atlas_tls_domain"],
		"region": frappe.conf.get("atlas_tls_region", get_region()),
		"access_key_id": values["atlas_route53_access_key_id"],
		"secret_access_key": values["atlas_route53_secret_access_key"],
		"account_email": values["atlas_acme_account_email"],
		"acme_directory_url": frappe.conf.get("atlas_acme_directory_url", LETS_ENCRYPT_STAGING),
		"aws_region": frappe.conf.get("atlas_route53_region", "us-east-1"),
	}


# --- Scaleway -------------------------------------------------------------
# The Scaleway provider e2e (atlas.tests.e2e.use_cases.scaleway_provisioning)
# is a separate billable path from the DO harness — it provisions a real
# Elastic Metal bare-metal server. Its inputs live in site config under
# atlas_scw_* keys, read here so the seed helper and the use case share them.


def get_scaleway_config() -> dict:
	"""Read the Scaleway inputs the provider e2e needs from site config.

	Raises `MissingConfig` naming the first absent required key, so a site that
	hasn't configured Scaleway skips the provider e2e cleanly rather than failing
	deep inside the API client.

	Keys:
	    atlas_scw_secret_key       IAM API key Secret Key (the X-Auth-Token value)
	    atlas_scw_project_id       Project UUID every resource is scoped to
	    atlas_scw_organization_id  optional; labels authenticate + filters projects
	    atlas_scw_zone             one EM zone (default fr-par-2 — A610R stock)
	    atlas_scw_size             Provider Size slug (default EM-A610R-NVME)
	    atlas_scw_image            Provider Image slug (default Ubuntu_24.04)
	    atlas_scw_billing          hourly | monthly (default hourly)
	"""
	required = ("atlas_scw_secret_key", "atlas_scw_project_id")
	values = {}
	for key in required:
		value = frappe.conf.get(key)
		if not value:
			raise MissingConfig(
				f"Scaleway e2e needs {key!r} in site config: bench --site <site> set-config -p {key} <value>"
			)
		values[key] = value
	return {
		"secret_key": values["atlas_scw_secret_key"],
		"project_id": values["atlas_scw_project_id"],
		"organization_id": frappe.conf.get("atlas_scw_organization_id"),
		"zone": frappe.conf.get("atlas_scw_zone", "fr-par-2"),
		"size": frappe.conf.get("atlas_scw_size", "EM-A610R-NVME"),
		"image": frappe.conf.get("atlas_scw_image", "Ubuntu_24.04"),
		"billing": frappe.conf.get("atlas_scw_billing", "hourly"),
	}


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


def control_plane_public_key() -> str:
	"""The public half of the key Atlas's control plane SSHes with — derived from
	`get_ssh_private_key_path()` (the same path `connection_for_guest` resolves via
	Atlas Settings).

	Most e2e VMs are reached only by host-side probes that carry the ephemeral key,
	so they provision with `ephemeral_public_key()` alone. The proxy VM is
	different: `atlas.atlas.proxy` (build/reconcile/cert) reaches the guest via
	`connection_for_guest`, which uses the Atlas-settings key — in production the
	proxy image bakes that key (the `connection_for_guest` contract). So a proxy VM
	must trust BOTH keys; provision it with `ephemeral_public_key() + "\\n" +
	control_plane_public_key()` (authorized_keys is one key per line)."""
	private_path = get_ssh_private_key_path()
	result = subprocess.run(
		["ssh-keygen", "-y", "-f", private_path],
		check=True,
		capture_output=True,
		text=True,
	)
	return result.stdout.strip()
