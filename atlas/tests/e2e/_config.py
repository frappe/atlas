"""E2E configuration object and stable test artifacts.

Every input the e2e harness needs (DO/Scaleway credentials, the SSH key, the
TLS account, the test region/size/image) comes from ONE explicit fixture file —
NOT `frappe.conf`. This is the test-side mirror of `atlas.setup.run(config)`:
the harness, like CI, drives the same explicit contract instead of reading site
config. `restore_credentials` re-applies this same fixture through the Layer-1
setters.

The fixture is a plain JSON document. Its path is `$ATLAS_E2E_CONFIG`, defaulting
to `~/.cache/atlas-e2e/config.json`. Fill it once per dev box; every phase shares
it. A missing file or absent required key raises `MissingConfig` naming what to
add, so a site that hasn't configured (say) TLS skips the TLS e2e cleanly rather
than failing deep inside certbot.

Example `~/.cache/atlas-e2e/config.json`::

    {
        "do_token": "dop_v1_…",
        "ssh_key_id": "12345678",
        "ssh_private_key_path": "~/.ssh/id_ed25519",
        "test_region": "blr1",
        "test_size": "s-2vcpu-4gb-intel",
        "test_image": "ubuntu-24-04-x64",
        "tls": {
            "domain": "blr1.frappe.dev",
            "region": "blr1",
            "route53_access_key_id": "AKIA…",
            "route53_secret_access_key": "…",
            "route53_region": "us-east-1",
            "acme_account_email": "ops@…",
            "acme_directory_url": "https://acme-staging-v02.api.letsencrypt.org/directory",
        },
        "scaleway": {
            "secret_key": "…",
            "project_id": "…",
            "organization_id": "…",
            "zone": "fr-par-2",
            "size": "EM-A610R-NVME",
            "image": "Ubuntu_24.04",
            "billing": "hourly",
        },
    }
"""

import json
import os
import subprocess

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

# Let's Encrypt staging — no rate limits, untrusted cert. The TLS e2e + the
# bootstrap TLS tail default here so a full producer pass (LE → DNS-01 → certbot)
# never burns production issuance quota. Override with tls.acme_directory_url.
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"

# Default test artifacts when the fixture omits them — DO's BLR region + a small
# Intel droplet + the noble server image. These are throwaway provisioning
# targets, so a sensible default keeps the fixture short.
_DEFAULT_REGION = "blr1"
_DEFAULT_SIZE = "s-2vcpu-4gb-intel"
_DEFAULT_IMAGE_SLUG = "ubuntu-24-04-x64"
_DEFAULT_SCW_ZONE = "fr-par-2"
_DEFAULT_SCW_SIZE = "EM-A610R-NVME"
_DEFAULT_SCW_IMAGE = "Ubuntu_24.04"


class MissingConfig(Exception):
	pass


# --- the explicit E2E config object -----------------------------------------

ENV_VAR = "ATLAS_E2E_CONFIG"
DEFAULT_PATH = "~/.cache/atlas-e2e/config.json"


def _config_path() -> str:
	return os.path.expanduser(os.environ.get(ENV_VAR) or DEFAULT_PATH)


class E2EConfig:
	"""The single explicit source of e2e inputs, loaded from the JSON fixture.

	One instance = one environment's config, the test-side analogue of the dict
	`atlas.setup.run` consumes. Readers below (`get_client`, `get_tls_config`, …)
	go through `load()` so nothing reaches into `frappe.conf`.
	"""

	def __init__(self, data: dict, source: str):
		self._data = data
		self._source = source

	@classmethod
	def load(cls) -> "E2EConfig":
		path = _config_path()
		if not os.path.isfile(path):
			raise MissingConfig(
				f"e2e config not found at {path!r}. Create it (or point {ENV_VAR} at it): "
				f"a JSON document with do_token / ssh_key_id / ssh_private_key_path "
				f"(see atlas/tests/e2e/_config.py for the full shape)."
			)
		with open(path) as handle:
			try:
				data = json.load(handle)
			except json.JSONDecodeError as exception:
				raise MissingConfig(f"e2e config {path!r} is not valid JSON: {exception}") from exception
		return cls(data, path)

	def require(self, key: str) -> str:
		"""Top-level required value; raise MissingConfig naming the key + the file."""
		value = self._data.get(key)
		if not value:
			raise MissingConfig(f"e2e config {self._source!r} is missing required key {key!r}")
		return value

	def get(self, key: str, default=None):
		return self._data.get(key, default)

	def section(self, key: str) -> dict:
		"""A nested block (`tls`, `scaleway`), or an empty dict if absent."""
		value = self._data.get(key) or {}
		if not isinstance(value, dict):
			raise MissingConfig(f"e2e config {self._source!r} key {key!r} must be an object")
		return value

	def require_in(self, section: str, key: str) -> str:
		"""A required value inside a nested block; raise naming both the block + key."""
		block = self.section(section)
		value = block.get(key)
		if not value:
			raise MissingConfig(
				f"e2e config {self._source!r} is missing {section}.{key} — needed for the {section} e2e"
			)
		return value


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
	return DigitalOceanClient(token=E2EConfig.load().require("do_token"))


def get_ssh_key_id() -> str:
	"""The vendor SSH-key id for e2e, from the fixture.

	The fixture is the source of truth for the e2e harness — Atlas Settings gets
	written *to* during `ensure_e2e_provider`, so reading from it would pick up
	stale values left by prior runs (e.g. unit-test fixtures' `key-id-123`)."""
	return E2EConfig.load().require("ssh_key_id")


def get_ssh_private_key_path() -> str:
	"""Absolute path on disk to the SSH private key for e2e.

	Reads `ssh_private_key_path` (a path on disk) or `ssh_private_key` (inline PEM
	we spill to a cache file). The fixture wins over Atlas Settings for the same
	reason as `get_ssh_key_id` — the Single is a write target during e2e setup."""
	config = E2EConfig.load()
	path = config.get("ssh_private_key_path")
	if path:
		expanded = os.path.expanduser(path)
		if not os.path.isfile(expanded):
			raise MissingConfig(f"ssh_private_key_path {path!r} is not a file")
		return expanded
	pem = config.get("ssh_private_key")
	if not pem:
		raise MissingConfig("e2e config needs ssh_private_key_path or ssh_private_key.")
	cache_dir = os.path.expanduser("~/.cache/atlas-e2e")
	os.makedirs(cache_dir, exist_ok=True)
	spilled = os.path.join(cache_dir, "provider-key.pem")
	with open(spilled, "w") as handle:
		handle.write(_load_key(pem))
	os.chmod(spilled, 0o600)
	return spilled


def get_tls_config() -> dict:
	"""Read the DNS + ACME inputs the TLS layer needs from the `tls` block.

	Raises `MissingConfig` naming the first absent key, so a fixture without a
	`tls` block skips the TLS e2e / bootstrap tail cleanly rather than failing
	deep inside certbot. Defaults to Route53 for existing fixtures; set
	`dns_provider_type = PowerDNS` plus the `powerdns_*` keys to exercise PowerDNS.
	"""
	config = E2EConfig.load()
	tls = config.section("tls")
	domain = config.require_in("tls", "domain")
	dns_provider_type = tls.get("dns_provider_type") or "Route53"
	result = {
		"domain": domain,
		"region": tls.get("region") or get_region(),
		"dns_provider_type": dns_provider_type,
		"account_email": config.require_in("tls", "acme_account_email"),
		"acme_directory_url": tls.get("acme_directory_url") or LETS_ENCRYPT_STAGING,
	}
	if dns_provider_type == "PowerDNS":
		result["powerdns"] = {
			"api_url": config.require_in("tls", "powerdns_api_url"),
			"api_key": config.require_in("tls", "powerdns_api_key"),
			"server_id": tls.get("powerdns_server_id") or "localhost",
		}
	else:
		result.update(
			{
				"access_key_id": config.require_in("tls", "route53_access_key_id"),
				"secret_access_key": config.require_in("tls", "route53_secret_access_key"),
				"aws_region": tls.get("route53_region") or "us-east-1",
			}
		)
	return result


def get_tls_domain() -> str | None:
	"""The TLS wildcard domain if the fixture configures one, else None.

	A thin accessor for teardown paths that only need the domain to find the rows
	to drop and must not raise when TLS is unconfigured."""
	return E2EConfig.load().section("tls").get("domain")


# --- Scaleway -------------------------------------------------------------
# The Scaleway provider e2e (atlas.tests.e2e.use_cases.scaleway_provisioning)
# is a separate billable path from the DO harness — it provisions a real
# Elastic Metal bare-metal server. Its inputs live in the fixture's `scaleway`
# block, read here so the seed helper and the use case share them.


def get_scaleway_config() -> dict:
	"""Read the Scaleway inputs the provider e2e needs from the `scaleway` block.

	Raises `MissingConfig` naming the first absent required key, so a fixture
	without a `scaleway` block skips the provider e2e cleanly rather than failing
	deep inside the API client. Keys (under `scaleway`):

	    secret_key       IAM API key Secret Key (the X-Auth-Token value)  [required]
	    project_id       Project UUID every resource is scoped to          [required]
	    organization_id  optional; labels authenticate + filters projects
	    zone             one EM zone (default fr-par-2 — A610R stock)
	    size             Provider Size slug (default EM-A610R-NVME)
	    image            Provider Image slug (default Ubuntu_24.04)
	    billing          hourly | monthly (default hourly)
	"""
	config = E2EConfig.load()
	scw = config.section("scaleway")
	return {
		"secret_key": config.require_in("scaleway", "secret_key"),
		"project_id": config.require_in("scaleway", "project_id"),
		"organization_id": scw.get("organization_id"),
		"zone": scw.get("zone") or _DEFAULT_SCW_ZONE,
		"size": scw.get("size") or _DEFAULT_SCW_SIZE,
		"image": scw.get("image") or _DEFAULT_SCW_IMAGE,
		"billing": scw.get("billing") or "hourly",
	}


def get_region() -> str:
	return E2EConfig.load().get("test_region") or _DEFAULT_REGION


def get_size() -> str:
	return E2EConfig.load().get("test_size") or _DEFAULT_SIZE


def get_image() -> str:
	return E2EConfig.load().get("test_image") or _DEFAULT_IMAGE_SLUG


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


# --- the contract: fixture → setup.run config -------------------------------


def setup_config() -> dict:
	"""Build the `atlas.setup.run` config dict from the E2E fixture.

	The test-side equivalent of `bootstrap.from_site_config()`: it turns the
	explicit fixture into the same `{provider, tls?, ...}` shape the Layer-1
	setters consume, so `restore_credentials` can re-seed the Singles through the
	real contract instead of writing fields by hand. The provider is always
	DigitalOcean here — the DO harness is the one that clobbers/needs the Singles
	restored; Scaleway is a separate billable path with its own seed.

	`region` is the Atlas single region (the source of truth) and `do.region` is
	DO's OWN API region; both happen to be the test region here, but they are set
	on different Singles by the setters (matching `from_site_config`'s split)."""
	config = E2EConfig.load()
	region = get_region()
	provider: dict = {
		"provider_type": "DigitalOcean",
		"region": region,
		"ssh_private_key_path": get_ssh_private_key_path(),
		"digitalocean": {
			"api_token": config.require("do_token"),
			"region": region,
			# bare vendor-native slugs; the DO setter prefixes "DigitalOcean/" itself.
			"default_size": get_size(),
			"default_image": get_image(),
			"ssh_key_id": config.require("ssh_key_id"),
		},
	}
	return {"provider": provider}
