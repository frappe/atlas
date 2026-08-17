"""A VM's disk-image operations — rebuilding a Stopped VM's disk from a snapshot
or a base image (spec/05-vm-lifecycle, spec/24).

Extracted from the `Virtual Machine` controller: re-imaging a VM's disk is one
cohesive reason to change (the disk-laydown payload), separate from the
create/power/terminate lifecycle. Free functions taking the VM, following the
`vm_provisioning.py` / `migration.py` pattern. The controller keeps a thin
`@whitelist rebuild` delegator (the Central/desk RPC surface + external Python
callers) and a thin `_rebuild_variables` delegator (`test_boat_lifecycle` calls
it on the doc).
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas import vm_provisioning
from atlas.atlas.networking import derive_uid


def rebuild(vm, source_type: str, source: str | None = None) -> str:
	"""Replace this Stopped VM's disk while keeping its identity.

	`source_type` is "snapshot" (restore one of this VM's own snapshots)
	or "image" (lay down a fresh rootfs from a base image; `source`
	defaults to the VM's current image). Name, IPv6, MAC, tap and SSH key
	are unchanged — only the disk bytes are swapped. The VM stays Stopped;
	the operator starts it when ready."""
	if vm.status != "Stopped":
		frappe.throw(f"Stop the VM before rebuilding (status is {vm.status})")
	vm._guard_no_active_migration()
	variables = rebuild_variables(vm, source_type, source)
	# A rebuild changes the disk, not the intent: the VM is Stopped before and
	# after, so the power stated is the one the row already carries.
	run = vm._transport()
	task = run(
		server=vm.server,
		script="rebuild-vm",
		variables=variables,
		virtual_machine=vm.name,
		timeout_seconds=300,
	)
	# rebuild-vm.py dropped any pending memory snapshot (saved RAM must never
	# be restored over a replaced disk); mirror that on the row.
	vm.db_set("has_memory_snapshot", 0)
	return task.name


def rebuild_variables(vm, source_type: str, source: str | None) -> dict:
	# Rebuild rewrites the guest's network env, so it must re-inject the
	# NAT44 v4 link or the rebuilt guest would boot with no v4 egress.
	#
	# An attached Reserved IP needs NOTHING here: rebuild swaps only the disk
	# and does not touch the host-side network.env, so its RESERVED_IPV4 line
	# (written by vm-reserved-ip.py at attach) survives the rebuild and the
	# 1:1-NAT is re-applied by vm-network-up.py on the next unit start. The
	# guest never sees the reserved IP either way (it binds only its /30).
	base = {
		"VIRTUAL_MACHINE_NAME": vm.name,
		"DISK_GB": str(vm.disk_gigabytes),
		"VIRTUAL_MACHINE_IPV6": vm.ipv6_address,
		"SSH_PUBLIC_KEY": vm_provisioning.guest_authorized_keys(vm),
		"ATLAS_FC_UID": str(derive_uid(vm.name)),
		**vm_provisioning.ipv4_link_variables(vm),
		# The private-plane /128 (spec/25). The rebuilt rootfs's network env is
		# written from scratch, so a VM whose address is not re-stated here comes
		# back OFF the private plane. Only the address: TENANT_PREFIX belongs to
		# the HOST's network.env, which a rebuild does not touch. Empty (a
		# tenant-less VM) is dropped by the Task runner.
		"PRIVATE_ADDRESS": vm_provisioning.private_network_variables(vm).get("PRIVATE_ADDRESS", ""),
		# The in-guest routing client's base URL (spec/18), for the same reason:
		# a rebuild-from-image lays down a rootfs that never carried the file, and
		# a bench VM without it can no longer register its own subdomains. Same
		# value provision injects; empty (no Satellite) is dropped.
		"ROUTING_BASE_URL": vm_provisioning.routing_base_url(),
		# Data-disk config so the rebuilt rootfs regains its fstab mount line.
		# DATA_DISK_MOUNT_AT is the one consumed on a rebuild-from-image (data
		# disk preserved); a restore also gets DATA_SNAPSHOT_ROOTFS_PATH below.
		**vm_provisioning.data_disk_variables(vm),
	}
	if source_type == "snapshot":
		if not source:
			frappe.throw(_("Rebuild from snapshot requires a snapshot"))
		snapshot = frappe.get_doc("Virtual Machine Snapshot", source)
		if snapshot.virtual_machine != vm.name:
			frappe.throw(_("Snapshot belongs to a different Virtual Machine"))
		if snapshot.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {snapshot.status})")
		# data_rootfs_path is empty when the snapshot captured no data disk;
		# the runner drops the empty flag and rebuild-vm.py leaves the live
		# data disk untouched (never silently destroys data).
		return {
			**base,
			"SNAPSHOT_ROOTFS_PATH": snapshot.rootfs_path,
			"DATA_SNAPSHOT_ROOTFS_PATH": snapshot.data_rootfs_path or "",
		}
	if source_type == "image":
		image_name = source or vm.image
		image = frappe.get_doc("Virtual Machine Image", image_name)
		return {
			**base,
			"IMAGE_NAME": image.image_name,
			"ROOTFS_FILENAME": image.rootfs_filename,
		}
	frappe.throw(f"Unknown rebuild source_type: {source_type!r}")
