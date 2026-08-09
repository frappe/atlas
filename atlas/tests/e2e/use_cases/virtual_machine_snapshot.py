"""Use case: snapshot, restore/rebuild, clone, resize, pause/resume a VM.

Operator extends a VM beyond the basic start/stop/terminate lifecycle:

- **Snapshot** a Stopped VM's disk into a Virtual Machine Snapshot row.
- **Restore** the snapshot back onto the same VM (rollback in place).
- **Rebuild** the VM's disk from its base image (wipe stored data).
- **Clone** the snapshot into a brand-new VM (fresh identity, disk seeded
  from the snapshot).
- **Resize** a Stopped VM (grow vCPU / memory / disk).
- **Pause / Resume** a Running VM via the Firecracker API socket.

All disk operations require the VM to be Stopped (consistent ext4); pause and
resume operate on a Running VM. This module runs the whole sequence against the
one shared bootstrapped droplet and probes the on-host outcome at each step.
"""

import frappe

from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_private_key,
	ephemeral_public_key,
	expect_validation_error,
	phase,
	wait_for_vm_running,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	run_against_shared(reuse=reuse, keep=keep)


def run_against_shared(reuse: bool = True, keep: bool = True) -> None:
	with phase("vm-snapshot", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		public_key = ephemeral_public_key()

		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "vm-snapshot",
				"server": server.name,
				"image": image_doc.name,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 4,
				# A data disk so snapshot/restore/resize/rebuild/clone all exercise
				# the root disk's peer; the marker round-trip below proves its data
				# survives a snapshot+restore and is carried into a clone.
				"data_disk_gigabytes": 2,
				"data_disk_format_and_mount": 1,
				"ssh_public_key": public_key,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		wait_for_vm_running(vm.name, timeout_seconds=120)
		vm.reload()

		_check_snapshot_and_restore(server.name, vm)
		_check_resize(server.name, vm)
		_check_rebuild_from_image(server.name, vm)
		clone = _check_clone(server.name, vm)
		_check_pause_resume(server.name, vm)  # leaves vm Stopped
		# Promote runs against the shared `vm` (which has a data disk) only to assert
		# the data-disk reject; the positive promote path provisions its own data-less
		# source VM inside the helper. Returns (source_vm, promoted_vm) to terminate.
		promote_source_vm, promoted_vm = _check_promote_to_image(server.name, vm)

		# Terminate both VMs; assert the snapshot files are gone with the VM dir.
		snapshot_paths = frappe.get_all(
			"Virtual Machine Snapshot",
			filters={"virtual_machine": vm.name},
			pluck="rootfs_path",
		)
		vm.terminate()
		for path in snapshot_paths:
			if path:
				assert_probe(server.name, "phase-snapshot-gone", SNAPSHOT_ROOTFS_PATH=path)
		assert not frappe.get_all("Virtual Machine Snapshot", filters={"virtual_machine": vm.name}), (
			"snapshot rows survived terminate"
		)

		clone.reload()
		clone.terminate()
		if promoted_vm is not None:
			promoted_vm.reload()
			promoted_vm.terminate()
			_cleanup_promoted_image(server.name, promoted_vm.image)
		if promote_source_vm is not None:
			promote_source_vm.reload()
			promote_source_vm.terminate()


# Every snapshot/restore/resize/rebuild/clone/pause/resume step is probed
# on-host; the unit-dup guards (shrink-rejected, pause-from-paused, clone
# guards) are cheap in-memory throws that ride along. The smoke path is the
# full run — there is no host work to trim without dropping a real disk op.
run_smoke = run_against_shared


def _check_snapshot_and_restore(server_name: str, vm) -> None:
	"""Stop -> Snapshot -> mutate -> Restore-onto-self -> Start, with on-host
	probes. Also proves the data disk round-trips: a marker written to /home
	before the snapshot survives a restore even after being overwritten."""
	# Seed a marker on the data disk while Running, then snapshot captures it.
	_write_data_marker(server_name, vm, "before-snapshot")

	vm.stop()
	vm.reload()
	assert vm.status == "Stopped", vm.status

	snapshot_name = vm.snapshot("vm-snapshot point-in-time")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
	assert snapshot.status == "Available", snapshot.status
	assert snapshot.size_bytes > 0, snapshot.size_bytes
	# The data disk was captured too: a second snapshot LV with its own bytes.
	assert snapshot.data_size_bytes > 0, snapshot.data_size_bytes
	assert snapshot.data_rootfs_path, "snapshot is missing data_rootfs_path"
	assert_probe(server_name, "phase-snapshot-present", SNAPSHOT_ROOTFS_PATH=snapshot.rootfs_path)
	assert_probe(server_name, "phase-snapshot-present", SNAPSHOT_ROOTFS_PATH=snapshot.data_rootfs_path)

	# Overwrite the marker so the restore has something to roll back.
	vm.start()
	vm.reload()
	assert vm.status == "Running", vm.status
	_write_data_marker(server_name, vm, "after-snapshot")
	vm.stop()
	vm.reload()

	# Restore the snapshot back onto the same VM (rollback in place).
	snapshot.restore_to_vm()
	vm.reload()
	assert vm.status == "Stopped", vm.status

	# Boot it and confirm identity is intact (same UUID-derived hostname etc.)...
	vm.start()
	vm.reload()
	assert vm.status == "Running", vm.status
	assert_probe(
		server_name,
		"phase5-guest-identity",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=vm.name,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)
	# ...and the data disk rolled back to the pre-snapshot marker (not the
	# "after-snapshot" overwrite), proving restore reverted the data, not just root.
	_expect_data_marker(server_name, vm, "before-snapshot")


def _check_resize(server_name: str, vm) -> None:
	"""Stop -> Resize (grow vCPU/mem/disk) -> assert config -> Start."""
	vm.stop()
	vm.reload()

	vm.resize(vcpus=2, memory_megabytes=1024, disk_gigabytes=6)
	vm.reload()
	assert vm.vcpus == 2 and vm.memory_megabytes == 1024 and vm.disk_gigabytes == 6
	assert_probe(
		server_name,
		"phase-resized-config",
		VIRTUAL_MACHINE_NAME=vm.name,
		VCPUS="2",
		MEMORY_MB="1024",
		DISK_GB="6",
	)

	# Shrink is rejected.
	with expect_validation_error("can only grow"):
		vm.resize(disk_gigabytes=4)

	vm.start()
	vm.reload()
	assert vm.status == "Running", vm.status
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)


def _check_rebuild_from_image(server_name: str, vm) -> None:
	"""Stop -> Rebuild from the base image -> Start, identity preserved."""
	vm.stop()
	vm.reload()
	vm.rebuild("image")
	vm.reload()
	assert vm.status == "Stopped", vm.status
	# Rebuild/restore now PRESERVE host keys (so a rollback never breaks clients'
	# known_hosts). A rebuild-from-image therefore carries the image's *shared*
	# baked host keys until rotated — so explicitly regenerate them here (the
	# opt-in action), which also makes the guest-identity probe's "no CI
	# build-container comment" check meaningful for this path.
	vm.regenerate_host_keys()
	vm.reload()
	assert vm.status == "Stopped", vm.status
	vm.start()
	vm.reload()
	assert_probe(
		server_name,
		"phase5-guest-identity",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=vm.name,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)
	# Rebuild rewrites the guest network env in place (atlas_inject_identity),
	# so prove the NAT44 v4 egress survives the rebuild — not just fresh
	# provision (spec/06-networking.md).
	assert_probe(
		server_name,
		"phase5-ipv4-egress",
		timeout_seconds=180,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)
	# Rebuild-from-image lays down a fresh root but PRESERVES the data disk (there
	# is no image source for it). The pre-snapshot marker must still be on /home,
	# and /home must still be a mounted ext4 (the fstab line was re-injected).
	_expect_data_marker(server_name, vm, "before-snapshot")
	assert_probe(
		server_name,
		"phase-data-disk",
		timeout_seconds=180,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
		MOUNT_AT="/home",
	)


def _check_clone(server_name: str, vm):
	"""Snapshot (from Stopped) -> clone into a new VM with fresh identity."""
	vm.stop()
	vm.reload()
	snapshot_name = vm.snapshot("vm-snapshot for clone")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)

	clone_name = snapshot.clone_to_new_vm(title="vm-snapshot clone", ssh_public_key=ephemeral_public_key())
	frappe.db.commit()
	clone = frappe.get_doc("Virtual Machine", clone_name)
	assert clone.name != vm.name
	assert clone.ipv6_address != vm.ipv6_address
	assert clone.clone_source_rootfs == snapshot.rootfs_path
	# The data disk clones too: same size, seeded from the snapshot's data half.
	assert clone.data_disk_gigabytes == vm.data_disk_gigabytes
	assert clone.clone_source_data_rootfs == snapshot.data_rootfs_path

	wait_for_vm_running(clone.name, timeout_seconds=120)
	clone.reload()
	# The clone boots with its own fresh identity (new hostname from new UUID,
	# regenerated host keys / machine-id) — the guest-identity probe asserts it.
	assert_probe(
		server_name,
		"phase5-guest-identity",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=clone.name,
		VIRTUAL_MACHINE_IPV6=clone.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)
	# The clone's data disk carries the source's data: the pre-snapshot marker is
	# on its /home (seeded from the data-disk snapshot, with a fresh ext4 UUID).
	_expect_data_marker(server_name, clone, "before-snapshot")

	# Bring the source VM back to Running so the pause/resume check has a
	# running target.
	vm.start()
	vm.reload()
	return clone


def _check_pause_resume(server_name: str, vm) -> None:
	"""Running -> Pause (API socket) -> assert paused -> Resume."""
	assert vm.status == "Running", vm.status
	vm.pause()
	vm.reload()
	assert vm.status == "Paused", vm.status
	assert_probe(server_name, "phase-is-paused", VIRTUAL_MACHINE_NAME=vm.name)

	# Pausing again from Paused is rejected by the controller guard.
	with expect_validation_error("cannot pause"):
		vm.pause()

	vm.resume()
	vm.reload()
	assert vm.status == "Running", vm.status
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# Stop it so the terminate at the end is from a quiet state.
	vm.stop()
	vm.reload()


def _check_promote_to_image(server_name: str, data_disk_vm):
	"""Promote a cold snapshot into a first-class base image, then provision a NEW
	VM that selects it via the ordinary `image` field and boot it (spec/08-images.md
	§ Two origins for a base image, spec/15-image-builder.md § Promoting a bake).

	The host facts only a droplet can prove: the dd'd rootfs LV is a read-only,
	sized block device; the image dir carries the reused kernel (hard-linked from
	the source image) + the rootfs sentinel; and a VM provisioned from the promoted
	image actually boots to a working guest with its own fresh identity.

	Promote is **root-only** — a base image has no data-disk fields — so we first
	assert the loud reject on `data_disk_vm` (the shared VM, which carries a data
	disk and is Stopped on entry), then provision a dedicated **data-less** source
	VM for the positive path. Returns (source_vm, promoted_vm) for teardown (either
	may be None if skipped)."""
	# 1. Negative: a snapshot with a data disk cannot be promoted (would drop data).
	assert data_disk_vm.status == "Stopped", data_disk_vm.status
	data_snapshot = frappe.get_doc(
		"Virtual Machine Snapshot", data_disk_vm.snapshot("vm-snapshot data for promote-reject")
	)
	assert data_snapshot.data_disk_gigabytes, "fixture VM should carry a data disk"
	try:
		data_snapshot.promote_to_image(image_name="should-reject-data")
		raise AssertionError("promote_to_image accepted a data-disk snapshot")
	except frappe.ValidationError as error:
		assert "data disk" in str(error), str(error)
	assert not frappe.db.exists("Virtual Machine Image", "should-reject-data")

	# 2. Positive: a data-less source VM, snapshotted Stopped, then promoted.
	source_vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "vm-promote-source",
			"server": server_name,
			"image": data_disk_vm.image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(source_vm.name, timeout_seconds=120)
	source_vm.reload()
	source_vm.stop()
	source_vm.reload()
	assert source_vm.status == "Stopped", source_vm.status

	snapshot = frappe.get_doc("Virtual Machine Snapshot", source_vm.snapshot("vm-snapshot for promote"))
	assert snapshot.kind == "Cold", snapshot.kind

	# A unique image name per run (the row name is the image name; reuse would clash
	# across re-runs of the shared-server e2e). Derive it from the snapshot UUID.
	image_name = f"promoted-{snapshot.name}".lower()
	source_image = frappe.get_doc("Virtual Machine Image", snapshot.source_image)
	returned = snapshot.promote_to_image(image_name=image_name, title="promoted by e2e")
	assert returned == image_name, returned

	image = frappe.get_doc("Virtual Machine Image", image_name)
	# A local image: URL-less, kernel inherited, non-syncable.
	assert image.is_local, "promoted image should be local (no rootfs URL)"
	assert not image.kernel_url and not image.rootfs_url, "promoted image has URLs"
	assert image.kernel_filename == source_image.kernel_filename
	assert image.default_disk_gigabytes == snapshot.disk_gigabytes

	# Host: the base image LV is a read-only sized block device; the image dir holds
	# the reused kernel + the rootfs sentinel — i.e. it looks like a synced image.
	assert_probe(
		server_name,
		"phase-promoted-image",
		IMAGE_NAME=image_name,
		ROOTFS_FILENAME=image.rootfs_filename,
		KERNEL_FILENAME=image.kernel_filename,
	)

	# Provision a brand-new VM that selects the promoted image via `image`. No
	# snapshot/clone path here — this is the ordinary base-image origin, proving the
	# promoted image is a first-class image new VMs boot from.
	promoted_vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "vm-from-promoted-image",
			"server": server_name,
			"image": image_name,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": snapshot.disk_gigabytes,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	wait_for_vm_running(promoted_vm.name, timeout_seconds=120)
	promoted_vm.reload()
	# It boots to a working guest with its own fresh, UUID-derived identity.
	assert_probe(
		server_name,
		"phase5-guest-identity",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=promoted_vm.name,
		VIRTUAL_MACHINE_IPV6=promoted_vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)
	return source_vm, promoted_vm


def _cleanup_promoted_image(server_name: str, image_name: str) -> None:
	"""e2e hygiene: drop the promoted base image LV + dir + row so a re-run of the
	shared-server suite doesn't accumulate atlas-image-promoted-* artifacts. A base
	image LV is PROTECTED (the lifecycle never removes it), so this uses a dedicated
	force probe — fine here because this image was minted by the e2e, not synced."""
	image_lv = f"atlas-image-{image_name}"
	assert_probe(server_name, "phase-remove-image", IMAGE_NAME=image_name, IMAGE_LV=image_lv)
	if frappe.db.exists("Virtual Machine Image", image_name):
		frappe.delete_doc("Virtual Machine Image", image_name, force=1, ignore_permissions=True)


def _write_data_marker(server_name: str, vm, marker: str) -> None:
	"""Write `marker` onto the VM's data disk (/home) over SSH."""
	assert_probe(
		server_name,
		"phase-data-marker",
		timeout_seconds=180,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
		MOUNT_AT="/home",
		MODE="write",
		MARKER=marker,
	)


def _expect_data_marker(server_name: str, vm, marker: str) -> None:
	"""Assert the VM's data disk (/home) carries `marker`."""
	assert_probe(
		server_name,
		"phase-data-marker",
		timeout_seconds=180,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
		MOUNT_AT="/home",
		MODE="expect",
		MARKER=marker,
	)
