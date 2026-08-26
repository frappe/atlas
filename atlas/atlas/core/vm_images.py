"""A VM's disk-image operations — snapshotting a VM's disk (cold or warm) into a
Virtual Machine Snapshot, and rebuilding a Stopped VM's disk from a snapshot or a
base image (spec/05-vm-lifecycle, spec/08, spec/24).

Extracted from the `Virtual Machine` controller: producing and laying down a VM's
disk image is one cohesive reason to change, separate from the create/power/
terminate lifecycle and from the machine-spec resizing in `vm_resize.py`. Free
functions taking the VM, following the `vm_provisioning.py` / `migration.py`
pattern. The whitelisted operations (snapshot, capture_warm_snapshot, rebuild)
stay as thin controller delegators — the Central/desk RPC surface + external
Python callers — and `_rebuild_variables` keeps a delegator too
(`test_boat_lifecycle` calls it on the doc).
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.core import vm_provisioning
from atlas.atlas.core.networking import derive_uid
from atlas.atlas.core.ssh import run_task
from atlas.atlas.core.task_results import parse_result


def snapshot(vm, title: str | None = None, live: bool = False) -> str:
	"""Snapshot this VM's disk(s) into a new Virtual Machine Snapshot row —
	the root disk and, if present, the data disk. Returns the snapshot's name.

	`title` is optional: omitted, it defaults to `<vm title> — <timestamp>`,
	so a caller (the SPA's one-click snapshot, or a direct API call) need not
	invent a name. The dashboard pre-fills the same default but lets the user
	edit it.

	Consistency — `live`:

	- Default (`live=False`): **Stopped-only**. A cleanly unmounted ext4 copies
	  flush-consistent, and with two disks a Stopped VM makes the root/data pair
	  mutually consistent. This is the safe default.
	- `live=True`: snapshot a **Running** (or Paused) VM without stopping. The
	  LVM thin CoW snapshot is atomic per volume, but the captured image is
	  **crash-consistent** — equivalent to pulling power at that instant:
	  unflushed guest-cache writes are absent and the guest replays its ext4
	  journal on next mount. The host can't quiesce the guest (no in-guest
	  agent), and the root/data LVs are snapshotted microseconds apart, so
	  cross-disk consistency isn't guaranteed. This is the same guarantee a
	  cloud "crash-consistent volume snapshot" gives; stop first for a
	  guaranteed-clean image."""
	# frm.call / REST send `live` as a JSON/stringy value; normalize to bool.
	live = live in (True, 1, "1", "true", "True", "yes")
	if vm.status == "Sleeping":
		frappe.throw(_("Cannot snapshot a Sleeping VM — wake it first, stop it, then snapshot"))
	if live:
		if vm.status not in ("Running", "Paused"):
			frappe.throw(
				f"Live snapshot needs a Running or Paused VM (status is {vm.status}); "
				f"for a Stopped VM take a normal snapshot"
			)
	elif vm.status != "Stopped":
		frappe.throw(
			f"Stop the VM before snapshotting (status is {vm.status}), "
			f"or pass live=True for a crash-consistent live snapshot"
		)
	vm._guard_no_active_migration()
	title = (title or "").strip() or default_snapshot_title(vm)
	# A snapshot captures BOTH disks: the data disk is a first-class peer of
	# root. We record its size + mount config on the row so a clone/restore can
	# reconstruct the data disk faithfully even if the source VM later changes.
	has_data = bool(vm.data_disk_gigabytes)
	snapshot = frappe.get_doc(
		{
			"doctype": "Virtual Machine Snapshot",
			"title": title,
			"virtual_machine": vm.name,
			"server": vm.server,
			"status": "Pending",
			"source_image": vm.image,
			"disk_gigabytes": vm.disk_gigabytes,
			"data_disk_gigabytes": vm.data_disk_gigabytes,
			"data_disk_mount_point": vm.data_disk_mount_point,
			"data_disk_format_and_mount": vm.data_disk_format_and_mount,
			# Carry the bench bake mode so a clone of this golden maps its FQDN to
			# the baked site (site) or the admin console (admin) — empty for an
			# ordinary VM snapshot (spec/08).
			"build_mode": vm.build_mode or None,
		}
	).insert(ignore_permissions=True)
	# The snapshot is an LVM thin snapshot, not a file copy. rootfs_path holds
	# its LV device path (derived from the snapshot's UUID, like the VM disk
	# LV) — no schema change, and it flows unchanged into restore/clone, which
	# read the LV name back from this path. The data snapshot LV is named off
	# the SAME snapshot UUID (atlas-datasnap-<id>), so the pair is recoverable.
	rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
	data_rootfs_path = f"/dev/atlas/atlas-datasnap-{snapshot.name}" if has_data else ""
	variables = {
		"VIRTUAL_MACHINE_NAME": vm.name,
		"SNAPSHOT_ROOTFS_PATH": rootfs_path,
	}
	if data_rootfs_path:
		variables["DATA_SNAPSHOT_ROOTFS_PATH"] = data_rootfs_path
	task = run_task(
		server=vm.server,
		script="snapshot-vm",
		variables=variables,
		virtual_machine=vm.name,
		timeout_seconds=300,
	)
	# One atomic update: the Task already succeeded and the on-host file
	# exists, so the row must end up Available. Folding the writes into a
	# single db_set means there's no window where rootfs_path/size_bytes
	# landed but status didn't (a half-update that stranded the row in
	# Pending). size_bytes is a Long Int / bigint column — a real multi-GB
	# rootfs overflows a plain Int.
	result = parse_result(task.stdout)
	snapshot.db_set(
		{
			"rootfs_path": rootfs_path,
			"size_bytes": result["size_bytes"],
			"data_rootfs_path": data_rootfs_path,
			"data_size_bytes": result.get("data_size_bytes", 0),
			"status": "Available",
		}
	)
	return snapshot.name


def default_snapshot_title(vm) -> str:
	"""`<vm title> — <YYYY-MM-DD HH:mm>` for an unnamed snapshot."""
	stamp = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M")
	return f"{vm.title} — {stamp}"


def capture_warm_snapshot(vm, title: str | None = None) -> str:
	"""Capture this live VM's memory AND disk at one paused instant into a new
	`kind=Warm` Virtual Machine Snapshot. Returns the snapshot's name.

	Named with a verb (not `warm_snapshot`) on purpose: `warm_snapshot` is the
	Link *field* that records the golden a warm clone was restored from, and a
	method of that name would be shadowed by the field value on a hydrated doc.

	The capture half of the Image Builder's warm bake
	(`image_build._warm_snapshot`), exposed as a per-VM operator action: pause
	the running guest's vCPUs, write the memory pair (`vmstate.bin` +
	`mem.bin`) and an LVM thin disk snapshot at the *same* paused instant to a
	durable per-snapshot directory, capture the host signature, then resume —
	the VM never stops. The frozen RAM references exactly those disk blocks, so
	the pair is only valid together (see
	[05-virtual-machine-lifecycle.md → Warm snapshot fan-out]).

	Running or Paused only (there is a live guest to freeze); a Stopped VM has
	no memory to capture — take a plain `snapshot()` instead. The capture
	script rejects a VM with a data disk (warm snapshots are root-only).

	The row records the captured machine config (vcpus, memory) and tap name —
	the vmstate pins all three, so a restore must reproduce them exactly. This
	action only *produces* the artifact; restoring it onto its own VM is the
	fast stop/start shape, and fanning it out into clones is safe only for a
	golden baked with the in-guest freshen unit (the Image Builder warm bake) —
	see `Virtual Machine Snapshot.clone_to_new_vm`."""
	if vm.status not in ("Running", "Paused"):
		frappe.throw(
			f"A warm snapshot needs a Running or Paused VM (status is {vm.status}); "
			f"for a Stopped VM take a plain snapshot"
		)
	vm._guard_no_active_migration()
	title = (title or "").strip() or default_snapshot_title(vm)
	snapshot = frappe.get_doc(
		{
			"doctype": "Virtual Machine Snapshot",
			"title": title,
			"virtual_machine": vm.name,
			"server": vm.server,
			"status": "Pending",
			"kind": "Warm",
			"source_image": vm.image,
			"disk_gigabytes": vm.disk_gigabytes,
			# Carry the bench bake mode (empty for an ordinary VM) so a clone of a
			# golden maps its FQDN correctly on first boot (spec/08).
			"build_mode": vm.build_mode or None,
			# The frozen vmstate pins the machine and its tap name; a warm clone
			# must reproduce all three exactly (clone_to_new_vm enforces it).
			"vcpus": vm.vcpus,
			"memory_megabytes": vm.memory_megabytes,
			"tap_device": vm.tap_device,
		}
	).insert(ignore_permissions=True)
	rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
	memory_directory = f"/var/lib/atlas/snapshots/{snapshot.name}"
	task = run_task(
		server=vm.server,
		script="warm-snapshot-vm",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"ATLAS_FC_UID": str(derive_uid(vm.name)),
			"SNAPSHOT_ROOTFS_PATH": rootfs_path,
			"MEMORY_DIRECTORY": memory_directory,
		},
		virtual_machine=vm.name,
		timeout_seconds=600,
	)
	# One atomic update, like snapshot(): the Task succeeded and the durable
	# artifacts exist on the host, so the row ends up Available with no window
	# where the paths landed but the status didn't.
	result = parse_result(task.stdout)
	snapshot.db_set(
		{
			"rootfs_path": rootfs_path,
			"size_bytes": result["size_bytes"],
			"memory_directory": memory_directory,
			"memory_bytes": result["memory_bytes"],
			"host_signature": result["host_signature"],
			"status": "Available",
		}
	)
	return snapshot.name


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
