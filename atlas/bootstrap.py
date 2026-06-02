"""Bootstrap a fresh Atlas site end-to-end.

Run with:

    bench --site <site> execute atlas.bootstrap.run

This sets up `Atlas Settings`, vendor-specific Settings, the active
`Provider` row, the size/image catalog rows, provisions a Server,
registers the default Virtual Machine Image, syncs it to the server,
and provisions one Virtual Machine. All inputs come from the site
config so the script takes no arguments.

A `bench worker` must be running — `provision_server` and `sync_to_server`
both enqueue background jobs that this script waits on.

Site config keys (set with `bench --site <site> set-config -p <key> <value>`):

    atlas_provider_type           "DigitalOcean" or "Self-Managed"
    atlas_ssh_private_key_path    absolute path to the SSH private key on disk
                                  (0600, readable by the Frappe user)
    atlas_ssh_fingerprint         vendor-side fingerprint of the SSH key
                                  (DigitalOcean only; required for DO)
    atlas_ssh_public_key          optional OpenSSH public key body for vendors
                                  that upload at provision time

DigitalOcean providers also need:

    atlas_do_token                DO personal access token
    atlas_do_region               e.g. "blr1"
    atlas_do_default_size         vendor-native slug, e.g. "s-2vcpu-4gb-intel"
                                  (Atlas prefixes "DigitalOcean/" internally)
    atlas_do_default_image        vendor-native slug, e.g. "ubuntu-24-04-x64"

Self-Managed providers also need:

    atlas_self_managed_ipv4                  the host's IPv4 (SSH endpoint)
    atlas_self_managed_ipv6                  the host's IPv6
    atlas_self_managed_ipv6_prefix           the prefix routed to the host
    atlas_self_managed_ipv6_vm_range         the subnet Atlas allocates VM IPs from

Optional VM inputs:

    atlas_vm_ssh_public_key       PEM contents or path to a public key
                                  (defaults to ~/.ssh/id_ed25519.pub)
"""

import os
import time

import frappe
import frappe.utils.password

PROVIDER_NAME = "bootstrap-provider"
IMAGE_NAME = "ubuntu-24.04"
MINIMAL_IMAGE_NAME = "ubuntu-24.04-minimal"

# Ubuntu cloud images (noble), pinned to a dated release for immutability.
# The dated path never changes under us; the floating `release/` pointer does.
# `kernel_sha256` is the digest of the DOWNLOADED packed vmlinuz — sync-image.sh
# decompresses the zstd payload to a raw vmlinux on the server (the extracted
# kernel is a derived artifact, not separately pinned). See spec/08-images.md.
_NOBLE_RELEASE = "https://cloud-images.ubuntu.com/releases/noble/release-20260518"
_NOBLE_MINIMAL_RELEASE = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260521"

DEFAULT_IMAGE = {
	"image_name": IMAGE_NAME,
	"title": "Ubuntu 24.04 server cloud image",
	"kernel_url": f"{_NOBLE_RELEASE}/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-server",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_RELEASE}/ubuntu-24.04-server-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-server.ext4",
	"rootfs_sha256": "bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74",
	"default_disk_gigabytes": 4,
}

# The minimal flavor lives under a different upstream tree and ships the same
# generic kernel as server (identical digest). Seeded as a second image row so
# operators can pick the smaller rootfs.
MINIMAL_IMAGE = {
	"image_name": MINIMAL_IMAGE_NAME,
	"title": "Ubuntu 24.04 minimal cloud image",
	"kernel_url": f"{_NOBLE_MINIMAL_RELEASE}/unpacked/ubuntu-24.04-minimal-cloudimg-amd64-vmlinuz-generic",
	"kernel_filename": "vmlinux-noble-minimal",
	"kernel_sha256": "3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6",
	"rootfs_url": f"{_NOBLE_MINIMAL_RELEASE}/ubuntu-24.04-minimal-cloudimg-amd64.squashfs",
	"rootfs_filename": "ubuntu-24.04-minimal.ext4",
	"rootfs_sha256": "a288f0bd499e1a747f86fda8ec9822dd99a4e3c0721d89ffd9dd57608ff21072",
	"default_disk_gigabytes": 4,
}


def run() -> None:
	"""End-to-end: settings → provider → server → image → virtual machine."""
	provider = ensure_provider()
	server_name = provision_server(provider)
	wait_for_active_server(server_name)
	ensure_image()
	sync_image(server_name)
	provision_virtual_machine(server_name)


def ensure_provider() -> "frappe.model.document.Document":
	provider_type = require_config("atlas_provider_type")
	if provider_type not in ("DigitalOcean", "Self-Managed"):
		frappe.throw(
			f"atlas_provider_type must be DigitalOcean or Self-Managed, got {provider_type!r}"
		)

	# Ensure the Provider row exists, then write the Singles.
	if not frappe.db.exists("Provider", PROVIDER_NAME):
		frappe.get_doc({
			"doctype": "Provider",
			"provider_name": PROVIDER_NAME,
			"provider_type": provider_type,
			"is_active": 1,
		}).insert(ignore_permissions=True)
		print(f"[bootstrap] created Provider {PROVIDER_NAME!r} ({provider_type})")
	else:
		print(f"[bootstrap] reusing Provider {PROVIDER_NAME!r}")

	# Atlas Settings — provider link + SSH triplet.
	frappe.db.set_single_value(
		"Atlas Settings", "provider", PROVIDER_NAME, update_modified=False
	)
	frappe.db.set_single_value(
		"Atlas Settings", "ssh_private_key_path",
		require_config("atlas_ssh_private_key_path"), update_modified=False,
	)
	if provider_type == "DigitalOcean":
		frappe.db.set_single_value(
			"Atlas Settings", "ssh_fingerprint",
			require_config("atlas_ssh_fingerprint"), update_modified=False,
		)
	public_key = frappe.conf.get("atlas_ssh_public_key")
	if public_key:
		frappe.db.set_single_value(
			"Atlas Settings", "ssh_public_key", public_key, update_modified=False
		)

	if provider_type == "DigitalOcean":
		region = require_config("atlas_do_region")
		size_slug = require_config("atlas_do_default_size")
		image_slug = require_config("atlas_do_default_image")

		# Seed the catalog rows the Settings will Link to.
		_ensure_provider_size(provider_type, size_slug)
		_ensure_provider_image(provider_type, image_slug)

		frappe.db.set_single_value("DigitalOcean Settings", "region", region, update_modified=False)
		frappe.db.set_single_value(
			"DigitalOcean Settings", "default_size",
			f"DigitalOcean/{size_slug}", update_modified=False,
		)
		frappe.db.set_single_value(
			"DigitalOcean Settings", "default_image",
			f"DigitalOcean/{image_slug}", update_modified=False,
		)
		frappe.utils.password.set_encrypted_password(
			"DigitalOcean Settings", "DigitalOcean Settings",
			require_config("atlas_do_token"), "api_token",
		)

		# Seed the wider catalog so the Refresh Catalog button is exercising
		# real data, not just the slugs the operator named in site config.
		from atlas.atlas.doctype.provider.provider import upsert_catalog
		from atlas.atlas.providers.digitalocean import DigitalOceanProvider

		try:
			capabilities = DigitalOceanProvider().discover()
			upsert_catalog(provider_type, capabilities)
		except Exception as exception:
			print(f"[bootstrap] WARN: catalog discover() failed: {exception}")

	frappe.db.commit()
	return frappe.get_doc("Provider", PROVIDER_NAME)


def _ensure_provider_size(provider_type: str, slug: str) -> None:
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Size", name):
		return
	import json
	frappe.get_doc({
		"doctype": "Provider Size",
		"provider_type": provider_type,
		"slug": slug,
		"enabled": 1,
		"provider_metadata": json.dumps({}),
	}).insert(ignore_permissions=True)


def _ensure_provider_image(provider_type: str, slug: str) -> None:
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Image", name):
		return
	import json
	frappe.get_doc({
		"doctype": "Provider Image",
		"provider_type": provider_type,
		"slug": slug,
		"enabled": 1,
		"provider_metadata": json.dumps({}),
	}).insert(ignore_permissions=True)


def provision_server(provider: "frappe.model.document.Document") -> str:
	title = f"bootstrap-server-{int(time.time())}"
	if provider.provider_type == "DigitalOcean":
		server_name = provider.provision_server(title)
	else:
		server_name = provider.provision_server(
			title,
			ipv4_address=require_config("atlas_self_managed_ipv4"),
			ipv6_address=require_config("atlas_self_managed_ipv6"),
			ipv6_prefix=require_config("atlas_self_managed_ipv6_prefix"),
			ipv6_virtual_machine_range=require_config("atlas_self_managed_ipv6_vm_range"),
		)
	frappe.db.commit()
	print(f"[bootstrap] provisioning Server {title!r} (name={server_name!r}; background job enqueued)")
	return server_name


def wait_for_active_server(server_name: str, timeout_seconds: int = 900) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Server", server_name, "status")
		print(f"[bootstrap] Server {server_name!r} status = {status}")
		if status == "Active":
			return
		if status == "Broken":
			frappe.throw(f"Server {server_name} ended in status Broken — check the Task list")
		time.sleep(10)
	frappe.throw(f"Server {server_name} did not become Active within {timeout_seconds}s")


def ensure_image() -> "frappe.model.document.Document":
	if frappe.db.exists("Virtual Machine Image", IMAGE_NAME):
		print(f"[bootstrap] reusing Virtual Machine Image {IMAGE_NAME!r}")
		return frappe.get_doc("Virtual Machine Image", IMAGE_NAME)
	image = frappe.get_doc({"doctype": "Virtual Machine Image", **DEFAULT_IMAGE, "is_active": 1}).insert(
		ignore_permissions=True
	)
	frappe.db.commit()
	print(f"[bootstrap] created Virtual Machine Image {image.name!r}")
	return image


def sync_image(server_name: str, timeout_seconds: int = 900) -> None:
	image = frappe.get_doc("Virtual Machine Image", IMAGE_NAME)
	task_name = image.sync_to_server(server_name)
	print(f"[bootstrap] syncing image to {server_name!r} (Task {task_name!r})")
	wait_for_task(task_name, timeout_seconds)


def provision_virtual_machine(server_name: str) -> str:
	virtual_machine = frappe.get_doc({
		"doctype": "Virtual Machine",
		"title": "bootstrap test vm",
		"server": server_name,
		"image": IMAGE_NAME,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": load_vm_ssh_public_key(),
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	print(f"[bootstrap] created Virtual Machine {virtual_machine.name!r}")
	task_name = _wait_for_provision_task(virtual_machine.name)
	print(f"[bootstrap] provisioning Virtual Machine (Task {task_name!r})")
	wait_for_task(task_name, timeout_seconds=300)
	return virtual_machine.name


def _wait_for_provision_task(virtual_machine_name: str, timeout_seconds: int = 60) -> str:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		rows = frappe.get_all(
			"Task",
			filters={
				"virtual_machine": virtual_machine_name,
				"script": "provision-vm.py",
			},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)
		if rows:
			return rows[0]
		time.sleep(2)
	frappe.throw(f"No provision Task appeared for {virtual_machine_name!r} within {timeout_seconds}s")


def wait_for_task(task_name: str, timeout_seconds: int) -> None:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		task = frappe.get_doc("Task", task_name)
		if task.status in ("Success", "Failure"):
			break
		time.sleep(5)
	else:
		frappe.throw(f"Task {task_name} did not finish within {timeout_seconds}s")
	if task.status != "Success":
		frappe.throw(f"Task {task_name} ended in {task.status}: {(task.stderr or '')[:500]}")


def require_config(key: str) -> str:
	value = frappe.conf.get(key)
	if not value:
		frappe.throw(
			f"site config missing {key!r}. Set with: bench --site <site> set-config -p {key} <value>"
		)
	return value


def load_key(value: str) -> str:
	"""Accept either inline PEM contents or a path to a key file."""
	if value.lstrip().startswith("-----BEGIN") or value.lstrip().startswith("ssh-"):
		return value
	path = os.path.expanduser(value)
	if not os.path.isfile(path):
		frappe.throw(f"key file not found at {path!r}")
	with open(path) as handle:
		return handle.read().strip()


def load_vm_ssh_public_key() -> str:
	configured = frappe.conf.get("atlas_vm_ssh_public_key")
	if configured:
		return load_key(configured)
	default_path = os.path.expanduser("~/.ssh/id_ed25519.pub")
	if not os.path.isfile(default_path):
		frappe.throw(
			"no SSH public key for the VM. Set atlas_vm_ssh_public_key in site "
			f"config or place one at {default_path!r}"
		)
	with open(default_path) as handle:
		return handle.read().strip()
