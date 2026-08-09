"""Shared test fixture builders.

Each builder implements "create if not exists" and accepts `**overrides`
that are merged into the doc dict before insert. Imported by unit tests in
`atlas/atlas/doctype/<x>/test_<x>.py` and `atlas/tests/test_*.py`.

Production code never touches these; they exist purely so test files don't
each carry a `_make_provider` reimplementation.
"""

import json
import pathlib
import tempfile
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import frappe
import frappe.utils.password
from frappe.model.document import Document

from atlas.tests.e2e._shared import DEFAULT_IMAGE

_FAKE_KEY_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"

DEFAULT_DIGITALOCEAN_SIZE = "DigitalOcean/s-2vcpu-4gb-intel"
DEFAULT_DIGITALOCEAN_IMAGE = "DigitalOcean/ubuntu-24-04-x64"


@contextmanager
def no_commit_enqueue():
	"""Patch frappe.enqueue AND frappe.db.commit for the duration of a call
	into a commit-before-enqueue code path (e.g. VirtualMachineImage.sync_to_server).

	That pattern commits before enqueuing so the background worker's separate
	connection can see the row — a real production need, but inside a test it
	flushes past IntegrationTestCase's rollback and leaks Server/Task rows
	permanently (they then compound: next run's Server.after_insert fan-out
	iterates over every previously-leaked Active server too). Patching only
	frappe.enqueue leaves the commit live; both must be patched together.

	Yields the enqueue Mock so callers can still assert on it."""
	with patch("frappe.db.commit"), patch("frappe.enqueue") as enqueue:
		yield enqueue


def _ensure_fake_ssh_key_path() -> str:
	"""Write a deterministic fake SSH key to a tempfile (one per session)
	and return its path. Tests that need to read the contents get a stable
	file; tests that only need `ssh_private_key_path` set get a real path."""
	path = pathlib.Path(tempfile.gettempdir()) / "atlas-test-ssh-key.pem"
	if not path.is_file():
		path.write_text(_FAKE_KEY_PEM)
		path.chmod(0o600)
	return str(path)


class _ProviderStub:
	"""Stand-in for the deleted `Provider` row. The Provider DocType is gone — the
	active vendor is `Atlas Settings.provider_type` — but a lot of tests still take a
	"provider" handle and read `.provider_type` (and a couple read `.name` /
	`.default_size`). This carries just those attributes so the fixtures' callers
	don't each have to change. `name` aliases `provider_type` (the only stable
	identifier left)."""

	def __init__(self, provider_type: str, default_size: str = "", default_image: str = ""):
		self.provider_type = provider_type
		self.name = provider_type
		self.default_size = default_size
		self.default_image = default_image


def make_provider_row(
	name: str = "test-provider",
	provider_type: str = "DigitalOcean",
	**overrides: Any,
) -> _ProviderStub:
	"""Set the active `Atlas Settings.provider_type` and return a provider stub.

	The `Provider` DocType is gone; `name` is accepted for call-site compatibility
	but ignored (there is no row to name). `fail_scripts` (Fake fault injection) now
	lives on the Single."""
	frappe.db.set_single_value("Atlas Settings", "provider_type", provider_type, update_modified=False)
	if "fail_scripts" in overrides:
		frappe.db.set_single_value(
			"Atlas Settings", "fail_scripts", overrides["fail_scripts"], update_modified=False
		)
	return _ProviderStub(provider_type)


def set_atlas_settings(
	provider: str | _ProviderStub,
	ssh_key_id: str | None = "key-id-123",
	ssh_private_key_path: str | None = None,
	ssh_public_key: str | None = None,
) -> None:
	"""Write Atlas Settings Single via set_single_value (bypasses reqd). The
	vendor-specific `ssh_key_id` handle lands on the active vendor's Single, not
	Atlas Settings — mirroring get_ssh_key()."""
	provider_type = provider.provider_type if isinstance(provider, _ProviderStub) else provider
	frappe.db.set_single_value("Atlas Settings", "provider_type", provider_type, update_modified=False)
	vendor_single = {"DigitalOcean": "DigitalOcean Settings", "Scaleway": "Scaleway Settings"}.get(
		provider_type
	)
	if ssh_key_id is not None and vendor_single:
		frappe.db.set_single_value(vendor_single, "ssh_key_id", ssh_key_id, update_modified=False)
	if ssh_public_key is not None:
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", ssh_public_key, update_modified=False)
	frappe.db.set_single_value(
		"Atlas Settings",
		"ssh_private_key_path",
		ssh_private_key_path or _ensure_fake_ssh_key_path(),
		update_modified=False,
	)


def set_digitalocean_settings(
	api_token: str = "dop_v1_fake",
	region: str = "blr1",
) -> None:
	"""Write DigitalOcean Settings Single. The default size/image are no longer
	fields here — `seed_catalogs()` marks the default catalog rows instead."""
	frappe.db.set_single_value("DigitalOcean Settings", "region", region, update_modified=False)
	# api_token is a Password field; route through the encryption helper.
	frappe.utils.password.set_encrypted_password(
		"DigitalOcean Settings", "DigitalOcean Settings", api_token, "api_token"
	)


def seed_catalogs() -> None:
	"""Seed the test catalog rows used by make_server and provider tests."""
	from atlas.atlas.providers.digitalocean import (
		DIGITALOCEAN_MONTHLY_COST_USD,
		KNOWN_DIGITALOCEAN_IMAGES,
		KNOWN_DIGITALOCEAN_SIZES,
	)

	for slug in KNOWN_DIGITALOCEAN_SIZES:
		name = f"DigitalOcean/{slug}"
		if frappe.db.exists("Provider Size", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Provider Size",
				"provider_type": "DigitalOcean",
				"slug": slug,
				"enabled": 1,
				"monthly_cost_usd": DIGITALOCEAN_MONTHLY_COST_USD.get(slug),
				"provider_metadata": json.dumps({}),
			}
		).insert(ignore_permissions=True)
	for slug in KNOWN_DIGITALOCEAN_IMAGES:
		name = f"DigitalOcean/{slug}"
		if frappe.db.exists("Provider Image", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Provider Image",
				"provider_type": "DigitalOcean",
				"slug": slug,
				"enabled": 1,
				"provider_metadata": json.dumps({}),
			}
		).insert(ignore_permissions=True)

	# Mark the canonical default so provisioning's catalog fallback resolves (the
	# desk modal and provision_server both read the is_default row). Idempotent.
	from atlas.atlas.setup_catalog import set_default

	if not frappe.db.get_value("Provider Size", {"provider_type": "DigitalOcean", "is_default": 1}, "name"):
		set_default("Provider Size", "DigitalOcean", DEFAULT_DIGITALOCEAN_SIZE.split("/", 1)[1])
	if not frappe.db.get_value("Provider Image", {"provider_type": "DigitalOcean", "is_default": 1}, "name"):
		set_default("Provider Image", "DigitalOcean", DEFAULT_DIGITALOCEAN_IMAGE.split("/", 1)[1])


def make_provider(name: str = "test-provider", **overrides: Any) -> _ProviderStub:
	"""Compatibility shim: set the active `Atlas Settings.provider_type`, seed
	DigitalOcean Settings + the test catalogs, and return a provider stub.

	The `Provider` DocType is gone; `name` is accepted but ignored. Callers still
	read `.provider_type` (and a couple `.default_size` / `.default_image`) off the
	returned stub."""
	provider_type = overrides.pop("provider_type", "DigitalOcean")
	# Strip legacy kwargs from the old Server Provider shape.
	for legacy in (
		"api_token",
		"ssh_key_id",
		"ssh_private_key_path",
		"default_region",
		"default_size",
		"default_image",
	):
		overrides.pop(legacy, None)

	seed_catalogs()
	provider = make_provider_row(name=name, provider_type=provider_type, **overrides)
	set_atlas_settings(provider)
	if provider_type == "DigitalOcean":
		set_digitalocean_settings()
		provider.default_size = DEFAULT_DIGITALOCEAN_SIZE
		provider.default_image = DEFAULT_DIGITALOCEAN_IMAGE
	return provider


def make_server(
	provider: _ProviderStub | None = None,
	title: str = "test-server",
	**overrides: Any,
) -> Document:
	"""Create a `Server` row if one with the given `title` does not exist."""
	existing = frappe.db.get_value("Server", {"title": title}, "name")
	if existing:
		server = frappe.get_doc("Server", existing)
		for field, value in overrides.items():
			if server.get(field) != value:
				frappe.db.set_value("Server", existing, field, value, update_modified=False)
		return frappe.get_doc("Server", existing)
	if provider is None:
		provider = make_provider()
	doc = {
		"doctype": "Server",
		"title": title,
		"provider_type": provider.provider_type,
		"provider_resource_id": None,
		"size": DEFAULT_DIGITALOCEAN_SIZE,
		"status": "Pending",
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def make_image(name: str = "test-image", **overrides: Any) -> Document:
	"""Create a `Virtual Machine Image` row if it doesn't already exist.

	The insert runs under `no_commit_enqueue()`: an active image's `after_insert`
	fans out a sync Task per Active server via the commit-before-enqueue path, and
	as a shared fixture (tests that need an image but not its sync) that would leak
	Server/Task rows past the rollback and pile real jobs onto `long` until the
	suite trips the queue-size guard. Tests that assert on the auto-sync fan-out
	build the image row inline under their own `no_commit_enqueue()` instead."""
	if frappe.db.exists("Virtual Machine Image", name):
		return frappe.get_doc("Virtual Machine Image", name)
	doc = {
		"doctype": "Virtual Machine Image",
		"image_name": name,
		"title": DEFAULT_IMAGE["title"],
		"kernel_url": "https://example.com/vmlinux",
		"kernel_filename": DEFAULT_IMAGE["kernel_filename"],
		"kernel_sha256": "a" * 64,
		"rootfs_url": "https://example.com/rootfs.squashfs",
		"rootfs_filename": DEFAULT_IMAGE["rootfs_filename"],
		"rootfs_sha256": "b" * 64,
		"default_disk_gigabytes": DEFAULT_IMAGE["default_disk_gigabytes"],
		"is_active": 1,
	}
	doc.update(overrides)
	with no_commit_enqueue():
		return frappe.get_doc(doc).insert(ignore_permissions=True)


def make_pilot(subdomain: str = "acme", vm_spec: dict | None = None, **overrides: Any) -> Document:
	"""Create a `Pilot` row. Its `after_insert` creates the backing VM synchronously
	from `vm_spec` (server/image/sizing), so the caller supplies at least a
	Fake-backed server + a bench image for the VM to inherit build_mode from."""
	doc = {"doctype": "Pilot", "subdomain": subdomain}
	doc.update(overrides)
	pilot = frappe.get_doc(doc)
	pilot.flags.vm_spec = vm_spec or {}
	return pilot.insert(ignore_permissions=True)


def make_virtual_machine(
	server: Document | str,
	image: Document | str,
	**overrides: Any,
) -> Document:
	"""Create a `Virtual Machine` row."""
	server_name = server.name if isinstance(server, Document) else server
	image_name = image.name if isinstance(image, Document) else image
	doc = {
		"doctype": "Virtual Machine",
		"title": "test vm",
		"server": server_name,
		"image": image_name,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 2,
		"ssh_public_key": "ssh-ed25519 AAAA",
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)
