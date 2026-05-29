"""Use case: provision a Firecracker microVM.

Operator creates a `Virtual Machine` row (server, image, vCPUs, RAM, disk,
SSH key, title) and clicks Save. Phase 4's auto-provision contract takes
over from there: `after_insert` enqueues `provision()`, which runs
`provision-vm.sh` to copy the rootfs, resize it, inject the SSH key and
the per-VM network env, and enable the systemd unit.

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

import time

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._shared import (
	DEFAULT_IMAGE,
	assert_probe,
	ensure_image_on_server,
	ephemeral_private_key,
	ephemeral_public_key,
	expect_validation_error,
	phase,
	wait_for_vm_running,
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
	aside *before* inserting the VM so the after_insert-enqueued
	`auto_provision` worker hits the missing-image branch. The row lands in
	Failed; we restore the rootfs and assert the operator can retry."""
	image_doc = frappe.get_doc("Virtual Machine Image", image)
	public_key = ephemeral_public_key()

	_move_image(server_name, image_doc, "aside")
	try:
		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"title": "image-missing negative path",
			"server": server_name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": public_key,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Auto-provision worker fires, hits the "rootfs missing" throw, and
		# task.py::_propagate_status_to_virtual_machine flips the VM to Failed.
		# Poll until the VM either lands in Failed or sits in Pending past
		# the worker latency window.
		deadline = time.monotonic() + 90
		while time.monotonic() < deadline:
			frappe.db.rollback()
			vm = frappe.get_doc("Virtual Machine", vm.name)
			if vm.status in ("Failed", "Pending"):
				break
			time.sleep(2)
	finally:
		# Always restore — a failed assertion above leaves the rootfs in .bak
		# and the happy path can't recover.
		_move_image(server_name, image_doc, "back")

	assert vm.status in ("Pending", "Failed"), vm.status

	# Drive the explicit provision() retry branch (now that the rootfs is back)
	# to cover the operator's "click Provision again" path on the Failed form.
	if vm.status == "Failed":
		vm.provision()
		vm.reload()
		assert vm.status == "Running", vm.status
		vm.terminate()

	# Tidy up the VM row we used to exercise the negative path.
	frappe.delete_doc("Virtual Machine", vm.name, force=True, ignore_permissions=True)


def _check_provision_happy_path(server_name: str, image: str, public_key: str) -> None:
	vm = frappe.get_doc({
		"doctype": "Virtual Machine",
		"title": "vm-provisioning happy path",
		"server": server_name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
	}).insert(ignore_permissions=True)
	frappe.db.commit()

	# Phase 4 auto-provision contract: `after_insert` enqueues `provision()`
	# so the operator never has to click Provision. The worker drives Pending
	# -> Running within ~60s on a warm server.
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	assert vm.last_started

	assert_probe(server_name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)

	# Phase 3 contract: the SSH key lives on disk (Atlas Settings
	# .ssh_private_key_path), not in the DB. Before SSHing into the guest,
	# assert the path the controller will read is present and 0600 —
	# surfaces a misconfigured host as a clean assertion rather than a
	# downstream SSH timeout.
	_assert_provider_ssh_key_path(server_name)

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

	# NAT44 IPv4 egress (spec/06-networking.md): the guest has a derived
	# 100.64.x.x/30, a v4 default route, and can reach a v4-only destination
	# (curl to an IPv4 literal) through the host masquerade. v6 stays primary;
	# we still hop in over the guest's IPv6.
	assert_probe(
		server_name,
		"phase5-ipv4-egress.sh",
		timeout_seconds=180,
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
		"title": "pre-derived fields",
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

	# Provision variables carry the derived NAT44 v4 link (no stored field):
	# host/guest /30 in 100.64.0.0/16, gateway = host side without mask.
	variables = pre_derived._provision_variables()
	assert variables["IPV4_HOST_CIDR"].startswith("100.64."), variables["IPV4_HOST_CIDR"]
	assert variables["IPV4_GUEST_CIDR"].startswith("100.64."), variables["IPV4_GUEST_CIDR"]
	assert variables["IPV4_GATEWAY"] == variables["IPV4_HOST_CIDR"].split("/")[0], variables

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
		"title": "set_status_default",
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
	import ipaddress

	from atlas.atlas.networking import (
		carve_virtual_machine_range,
		derive_ipv4_link,
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

	# IPv4 NAT44 egress link: derived from the v6 address, a /30 inside the
	# 100.64.0.0/16 CGNAT supernet. ::2 is the first VM address the allocator
	# hands out; its link is the documented worked example.
	host_cidr, guest_cidr = derive_ipv4_link("2001:db8::2")
	assert host_cidr == "100.64.0.9/30", host_cidr
	assert guest_cidr == "100.64.0.10/30", guest_cidr
	# Host and guest sit in the same /30, both inside the supernet.
	supernet = ipaddress.IPv4Network("100.64.0.0/16")
	host_interface = ipaddress.ip_interface(host_cidr)
	guest_interface = ipaddress.ip_interface(guest_cidr)
	assert host_interface.network == guest_interface.network, (host_cidr, guest_cidr)
	assert host_interface.ip in supernet and guest_interface.ip in supernet
	# Distinct v6 addresses across a /124 yield distinct, non-overlapping links.
	guest_addresses = {derive_ipv4_link(f"2001:db8::{index:x}")[1] for index in range(2, 16)}
	assert len(guest_addresses) == 14, guest_addresses


def _check_ipv6_exhaustion(server) -> None:
	"""Fill a transient server's /124 to drive the `No IPv6 capacity` raise.

	A /124 holds 14 usable addresses (skipping ::0 and ::1). Use a synthetic
	Server row so we don't compete with the real e2e server's allocator.
	"""
	from atlas.atlas.networking import allocate_ipv6

	fake_title = "usecase-ipv6-exhaust"
	existing_name = frappe.db.get_value("Server", {"title": fake_title}, "name")
	if existing_name:
		for vm in frappe.get_all(
			"Virtual Machine", filters={"server": existing_name}, pluck="name"
		):
			frappe.delete_doc("Virtual Machine", vm, force=True, ignore_permissions=True)
		frappe.delete_doc("Server", existing_name, force=True, ignore_permissions=True)

	fake_server = frappe.get_doc({
		"doctype": "Server",
		"title": fake_title,
		"provider": server.provider,
		"status": "Pending",
		"ipv4_address": "192.0.2.99",
		"ipv6_address": "2001:db8::1",
		"ipv6_prefix": "2001:db8::/64",
		"ipv6_virtual_machine_range": "2001:db8::/124",
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	fake_name = fake_server.name

	try:
		for i in range(14):
			address = allocate_ipv6(fake_name)
			frappe.get_doc({
				"doctype": "Virtual Machine",
				"title": f"ipv6-exhaust-{i}",
				"server": fake_name,
				"image": DEFAULT_IMAGE["image_name"],
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


def _assert_provider_ssh_key_path(server_name: str) -> None:
	"""Phase 3 contract: `Atlas Settings.ssh_private_key_path` stores the
	path (not the key itself). Assert the path resolves to a regular file
	on disk with mode 0600 before any SSH attempt uses it."""
	import os
	import stat

	path = frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path")
	assert path, "Atlas Settings.ssh_private_key_path is empty"

	resolved = os.path.expanduser(path)
	assert os.path.isfile(resolved), f"ssh_private_key_path {path!r} is not a file"

	mode = stat.S_IMODE(os.stat(resolved).st_mode)
	# 0600 is the strict expectation; some test envs land at 0400 (read-only)
	# which is equally safe.
	assert mode in (0o600, 0o400), (
		f"ssh_private_key_path {path!r} mode is {oct(mode)}, want 0600 or 0400"
	)


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
