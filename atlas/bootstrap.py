"""Bootstrap a fresh Atlas site end-to-end.

Run with:

    bench --site <site> execute atlas.bootstrap.run

This creates a Server Provider, provisions a Server, registers the default
Virtual Machine Image, syncs it to the server, and provisions one Virtual
Machine. All inputs come from the site config so the script takes no
arguments.

A `bench worker` must be running — `provision_server` and `sync_to_server`
both enqueue background jobs that this script waits on.

Site config keys (set with `bench --site <site> set-config -p <key> <value>`):

    atlas_provider_type           "DigitalOcean" or "Self-Managed"
    atlas_ssh_private_key_path    absolute path to the SSH private key on disk
                                  (0600, readable by the Frappe user)

DigitalOcean providers also need:

    atlas_do_token                DO personal access token
    atlas_ssh_key_id              fingerprint of the SSH key pre-loaded on droplets
    atlas_default_region          e.g. "blr1"
    atlas_default_size            e.g. "s-2vcpu-4gb-intel"
    atlas_default_image           e.g. "ubuntu-24-04-x64"

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

PROVIDER_NAME = "bootstrap-provider"
IMAGE_NAME = "ubuntu-24.04"

DEFAULT_IMAGE = {
	"image_name": IMAGE_NAME,
	"title": "Firecracker CI Ubuntu 24.04 rootfs",
	"kernel_url": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128",
	"kernel_filename": "vmlinux-6.1.128",
	"kernel_sha256": "27a8310b9a727517e9eb02044524b6ceb77de5728e3491b6974d5c846227ecc8",
	"rootfs_url": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/ubuntu-24.04.squashfs",
	"rootfs_filename": "ubuntu-24.04.ext4",
	"rootfs_sha256": "88821a26b5a38c92b84a064d452167d7f80f9e17cf4441d1ebbae7569e340aee",
	"default_disk_gigabytes": 4,
}


def run() -> None:
	"""End-to-end: provider → server → image → virtual machine."""
	provider = ensure_provider()
	server_name = provision_server(provider)
	wait_for_active_server(server_name)
	ensure_image()
	sync_image(server_name)
	provision_virtual_machine(server_name)


def ensure_provider() -> "frappe.model.document.Document":
	provider_type = require_config("atlas_provider_type")
	if provider_type not in ("DigitalOcean", "Self-Managed"):
		frappe.throw(f"atlas_provider_type must be DigitalOcean or Self-Managed, got {provider_type!r}")

	if frappe.db.exists("Server Provider", PROVIDER_NAME):
		print(f"[bootstrap] reusing Server Provider {PROVIDER_NAME!r}")
		return frappe.get_doc("Server Provider", PROVIDER_NAME)

	values = {
		"doctype": "Server Provider",
		"provider_name": PROVIDER_NAME,
		"provider_type": provider_type,
		"is_active": 1,
		"ssh_private_key_path": require_config("atlas_ssh_private_key_path"),
	}
	if provider_type == "DigitalOcean":
		values.update({
			"api_token": require_config("atlas_do_token"),
			"ssh_key_id": require_config("atlas_ssh_key_id"),
			"default_region": require_config("atlas_default_region"),
			"default_size": require_config("atlas_default_size"),
			"default_image": require_config("atlas_default_image"),
		})

	provider = frappe.get_doc(values).insert(ignore_permissions=True)
	frappe.db.commit()
	print(f"[bootstrap] created Server Provider {provider.name!r} ({provider_type})")
	return provider


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
	# `after_insert` enqueues `auto_provision` so we don't have to call
	# `provision()` explicitly. Pull the most recent provision-vm Task for
	# this VM out of the queue and wait on it.
	task_name = _wait_for_provision_task(virtual_machine.name)
	print(f"[bootstrap] provisioning Virtual Machine (Task {task_name!r})")
	wait_for_task(task_name, timeout_seconds=300)
	return virtual_machine.name


def _wait_for_provision_task(virtual_machine_name: str, timeout_seconds: int = 60) -> str:
	"""Block until the after_insert worker has created the provision Task
	row, then return its name. We poll the Task list rather than sleep on a
	hard delay because the worker latency is short but non-zero."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		rows = frappe.get_all(
			"Task",
			filters={
				"virtual_machine": virtual_machine_name,
				"script": "provision-vm.sh",
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
		frappe.throw(f"site config missing {key!r}. Set with: bench --site <site> set-config -p {key} <value>")
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
