"""Use case: provision a Firecracker microVM.

Operator creates a `Virtual Machine` row (server, image, vCPUs, RAM, disk,
SSH key, description) and clicks "Provision". The button runs
`provision-vm.sh`, which copies the rootfs, resizes it, injects the SSH key
and the per-VM network env, and enables the systemd unit.

This module exercises:

- Happy path: provision, assert the VM boots, the systemd unit is active.
- Image absent: provision-vm.sh exits non-zero with a "run Sync to Server"
  hint; the row stays at Pending and is re-provisionable.
- Derived-field defaults: `mac_address`, `tap_device`, `ipv6_address` are
  computed in `before_validate`; pre-supplied values are honored.
- Immutability of `server` / `image` / `vcpus` / `memory_megabytes` /
  `disk_gigabytes` after insert.
- IPv6 allocator capacity: a /124 holds 14 usable addresses; the 15th
  allocation throws.
- Pure networking helpers (`carve_virtual_machine_range`, `derive_mac`,
  `derive_tap`).
"""

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_private_key,
	ephemeral_public_key,
	expect_validation_error,
	phase,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	with phase("vm-provisioning", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		image = image_doc.name
		public_key = ephemeral_public_key()

		_check_provision_image_missing(server.name, image)
		_check_provision_happy_path(server.name, image, public_key)
		_check_derived_fields_and_immutability(server.name, image, public_key)
		_check_networking_helpers()
		_check_ipv6_exhaustion(server)


def _check_provision_image_missing(server_name: str, image: str) -> None:
	"""provision-vm.sh step 0: rootfs must already exist on the host. Move it
	aside, attempt to provision, expect a ValidationError, then restore."""
	image_doc = frappe.get_doc("Virtual Machine Image", image)
	public_key = ephemeral_public_key()

	vm = frappe.get_doc({
		"doctype": "Virtual Machine",
		"description": "image-missing negative path",
		"server": server_name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
	}).insert(ignore_permissions=True)

	_move_image(server_name, image_doc, "aside")
	try:
		with expect_validation_error("not present", "missing"):
			vm.provision()
	finally:
		# Always restore — a failed assertion above leaves the rootfs in .bak
		# and the happy path can't recover.
		_move_image(server_name, image_doc, "back")

	# Failed provision lands in either Pending (run_task raised before any
	# status flip) or Failed (Task.on_update propagated Failure to the VM,
	# see task.py::_propagate_status_to_virtual_machine). Both are
	# re-provisionable by Virtual Machine.provision(), so the operator can
	# click Provision again after running Sync to Server.
	vm.reload()
	assert vm.status in ("Pending", "Failed"), vm.status

	# Tidy up the VM row we used to exercise the negative path.
	frappe.delete_doc("Virtual Machine", vm.name, force=True, ignore_permissions=True)


def _check_provision_happy_path(server_name: str, image: str, public_key: str) -> None:
	vm = frappe.get_doc({
		"doctype": "Virtual Machine",
		"description": "vm-provisioning happy path",
		"server": server_name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
	}).insert(ignore_permissions=True)

	vm.provision()
	vm.reload()
	assert vm.status == "Running", vm.status
	assert vm.last_started

	assert_probe(server_name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)

	# SSH into the guest over its IPv6 and assert the fit-and-finish
	# guarantees from llm/plan/real-vm-fitfinish.md: per-VM hostname,
	# regenerated machine-id and ssh host keys, no fcnet IPv4 leftover,
	# clean /etc/hosts, locked root, sshd password-auth off, swap on.
	assert_probe(
		server_name,
		"phase5-guest-identity.sh",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=vm.name,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)

	# Provision again from Running -> throw (cleanup happens via the test's
	# server teardown — this row stays Running so the lifecycle use case can
	# inherit it if it cares, but in practice each use case owns its own VM).
	with expect_validation_error("cannot provision"):
		vm.provision()

	# Terminate so we don't accumulate Running VMs on the shared server.
	vm.terminate()


def _check_derived_fields_and_immutability(server_name: str, image: str, public_key: str) -> None:
	"""Pre-supplied mac/tap/ipv6 are honored; resource fields are immutable."""
	# Pre-derived values pass through (covers `if not self.x:` false branch
	# in before_validate).
	pre_derived = frappe.get_doc({
		"doctype": "Virtual Machine",
		"description": "pre-derived fields",
		"server": server_name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
		"mac_address": "06:00:de:ad:be:ef",
		"tap_device": "atlas-deadbeef",
		"ipv6_address": "fd00::dead",
	}).insert(ignore_permissions=True)
	assert pre_derived.mac_address == "06:00:de:ad:be:ef"
	assert pre_derived.tap_device == "atlas-deadbeef"
	assert pre_derived.ipv6_address == "fd00::dead"

	# Mutate vcpus after insert -> throw.
	pre_derived.vcpus = 99
	with expect_validation_error("immutable"):
		pre_derived.save(ignore_permissions=True)
	pre_derived.reload()

	# validate() on a freshly-loaded doc (no prior save) early-returns when
	# `_doc_before_save` is None; call directly to drive that branch.
	fresh = frappe.get_doc("Virtual Machine", pre_derived.name)
	fresh.validate()

	# set_status_default helper: the JSON schema sets the default before
	# before_insert runs, so the assignment in set_status_default is dead in
	# the insert() flow. Call the helper directly with a cleared field.
	transient = frappe.get_doc({
		"doctype": "Virtual Machine",
		"description": "set_status_default",
		"server": server_name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
	})
	transient.status = None
	transient.set_status_default()
	assert transient.status == "Pending"

	# Cleanup.
	pre_derived.status = "Terminated"
	pre_derived.save(ignore_permissions=True)


def _check_networking_helpers() -> None:
	"""Pure-Python helpers: cheap to exercise, expensive to leave uncovered."""
	from atlas.atlas.networking import (
		carve_virtual_machine_range,
		derive_mac,
		derive_tap,
	)

	cidr = carve_virtual_machine_range(
		"2604:a880:cad:d0:0:1:4ae1:d001", "2604:a880:cad:d0::/64"
	)
	assert cidr.endswith("/124"), cidr

	sample_uuid = "550e8400-e29b-41d4-a716-446655440000"
	mac = derive_mac(sample_uuid)
	assert mac.startswith("06:00:"), mac
	tap = derive_tap(sample_uuid)
	assert tap.startswith("atlas-") and len(tap) == 15, tap


def _check_ipv6_exhaustion(server) -> None:
	"""Fill a transient server's /124 to drive the `No IPv6 capacity` raise.

	A /124 holds 14 usable addresses (skipping ::0 and ::1). Use a synthetic
	Server row so we don't compete with the real e2e server's allocator.
	"""
	from atlas.atlas.networking import allocate_ipv6

	fake_name = "usecase-ipv6-exhaust"
	if frappe.db.exists("Server", fake_name):
		for vm in frappe.get_all(
			"Virtual Machine", filters={"server": fake_name}, pluck="name"
		):
			frappe.delete_doc("Virtual Machine", vm, force=True, ignore_permissions=True)
		frappe.delete_doc("Server", fake_name, force=True, ignore_permissions=True)

	frappe.get_doc({
		"doctype": "Server",
		"server_name": fake_name,
		"provider": server.provider,
		"status": "Pending",
		"ipv4_address": "192.0.2.99",
		"ipv6_address": "2001:db8::1",
		"ipv6_prefix": "2001:db8::/64",
		"ipv6_virtual_machine_range": "2001:db8::/124",
	}).insert(ignore_permissions=True)
	frappe.db.commit()

	try:
		for _ in range(14):
			address = allocate_ipv6(fake_name)
			frappe.get_doc({
				"doctype": "Virtual Machine",
				"server": fake_name,
				"image": "ubuntu-24.04",
				"vcpus": 1,
				"memory_megabytes": 256,
				"disk_gigabytes": 1,
				"ssh_public_key": "ssh-rsa AAA",
				"ipv6_address": address,
				"status": "Running",
			}).insert(ignore_permissions=True)
		with expect_validation_error("no ipv6 capacity"):
			allocate_ipv6(fake_name)
	finally:
		for vm in frappe.get_all(
			"Virtual Machine", filters={"server": fake_name}, pluck="name"
		):
			frappe.delete_doc("Virtual Machine", vm, force=True, ignore_permissions=True)
		frappe.delete_doc("Server", fake_name, force=True, ignore_permissions=True)
		frappe.db.commit()


def _move_image(server_name: str, image_doc, direction: str) -> None:
	assert direction in {"aside", "back"}, direction
	task = run_task(
		server=server_name,
		script="phase5-move-image.sh",
		variables={
			"IMAGE_NAME": image_doc.image_name,
			"ROOTFS_FILENAME": image_doc.rootfs_filename,
			"DIRECTION": direction,
		},
		timeout_seconds=15,
	)
	assert task.status == "Success"
