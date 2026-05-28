"""Shared test fixture builders.

Each builder implements "create if not exists" and accepts `**overrides`
that are merged into the doc dict before insert. Imported by unit tests in
`atlas/atlas/doctype/<x>/test_<x>.py` and `atlas/tests/test_*.py`.

Production code never touches these; they exist purely so test files don't
each carry a `_make_provider` reimplementation.
"""

import pathlib
import tempfile
from typing import Any

import frappe
from frappe.model.document import Document

from atlas.tests.e2e._shared import DEFAULT_IMAGE


_FAKE_KEY_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"


def _ensure_fake_ssh_key_path() -> str:
	"""Write a deterministic fake SSH key to a tempfile (one per session)
	and return its path. Tests that need to read the contents get a stable
	file; tests that only need `ssh_private_key_path` set get a real path."""
	path = pathlib.Path(tempfile.gettempdir()) / "atlas-test-ssh-key.pem"
	if not path.is_file():
		path.write_text(_FAKE_KEY_PEM)
		path.chmod(0o600)
	return str(path)


def make_provider(name: str = "test-provider", **overrides: Any) -> Document:
	"""Create a `Server Provider` row if it doesn't already exist.

	Token shape pinned to `dop_v1_*` so callers see what the production DO
	library expects.
	"""
	if frappe.db.exists("Server Provider", name):
		return frappe.get_doc("Server Provider", name)
	doc = {
		"doctype": "Server Provider",
		"provider_name": name,
		"provider_type": "DigitalOcean",
		"api_token": "dop_v1_fake",
		"ssh_key_id": "fp:fingerprint",
		"ssh_private_key_path": _ensure_fake_ssh_key_path(),
		"default_region": "blr1",
		"default_size": "s-2vcpu-4gb-intel",
		"default_image": "ubuntu-24-04-x64",
		"is_active": 1,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def make_server(
	provider: Document | None = None,
	title: str = "test-server",
	**overrides: Any,
) -> Document:
	"""Create a `Server` row if one with the given `title` does not exist.

	`Server.name` is a UUID assigned by the controller's `autoname()`;
	tests look up by `title` so they stay deterministic across runs. If
	`provider` is omitted, a default test provider is created.

	If a row with the given title already exists, mutable fields named in
	`overrides` (typically `status`) are written back so the row matches the
	caller's expectation even across test runs that left polluted state.
	"""
	existing = frappe.db.get_value("Server", {"title": title}, "name")
	if existing:
		server = frappe.get_doc("Server", existing)
		# Mutable fields that tests routinely override (`status`) need to be
		# reapplied; immutable fields are silently ignored.
		for field, value in overrides.items():
			if server.get(field) != value:
				frappe.db.set_value("Server", existing, field, value, update_modified=False)
		return frappe.get_doc("Server", existing)
	if provider is None:
		provider = make_provider()
	doc = {
		"doctype": "Server",
		"title": title,
		"provider": provider.name,
		"provider_resource_id": None,
		"region": provider.default_region,
		"size": provider.default_size,
		"status": "Pending",
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def make_image(name: str = "test-image", **overrides: Any) -> Document:
	"""Create a `Virtual Machine Image` row if it doesn't already exist.

	Defaults mirror `DEFAULT_IMAGE` from the e2e shared module — same field
	shapes, but the URLs/SHAs are placeholders that never get fetched in
	unit tests.
	"""
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
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def make_virtual_machine(
	server: Document | str,
	image: Document | str,
	**overrides: Any,
) -> Document:
	"""Create a `Virtual Machine` row.

	`Virtual Machine.name` is assigned by the controller's `before_insert`
	via `uuid.uuid4()`, so there is no "if exists, return" shortcut: every
	call inserts a fresh row.
	"""
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
