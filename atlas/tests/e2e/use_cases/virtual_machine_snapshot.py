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

		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"title": "vm-snapshot",
			"server": server.name,
			"image": image_doc.name,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": public_key,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		wait_for_vm_running(vm.name, timeout_seconds=120)
		vm.reload()

		_check_snapshot_and_restore(server.name, vm)
		_check_resize(server.name, vm)
		_check_rebuild_from_image(server.name, vm)
		clone = _check_clone(server.name, vm)
		_check_pause_resume(server.name, vm)

		# Terminate both VMs; assert the snapshot files are gone with the VM dir.
		snapshot_paths = frappe.get_all(
			"Virtual Machine Snapshot",
			filters={"virtual_machine": vm.name},
			pluck="rootfs_path",
		)
		vm.terminate()
		for path in snapshot_paths:
			if path:
				assert_probe(server.name, "phase-snapshot-gone.sh", SNAPSHOT_ROOTFS_PATH=path)
		assert not frappe.get_all(
			"Virtual Machine Snapshot", filters={"virtual_machine": vm.name}
		), "snapshot rows survived terminate"

		clone.reload()
		clone.terminate()


# Every snapshot/restore/resize/rebuild/clone/pause/resume step is probed
# on-host; the unit-dup guards (shrink-rejected, pause-from-paused, clone
# guards) are cheap in-memory throws that ride along. The smoke path is the
# full run — there is no host work to trim without dropping a real disk op.
run_smoke = run_against_shared


def _check_snapshot_and_restore(server_name: str, vm) -> None:
	"""Stop -> Snapshot -> Restore-onto-self -> Start, with on-host probes."""
	vm.stop()
	vm.reload()
	assert vm.status == "Stopped", vm.status

	snapshot_name = vm.snapshot("vm-snapshot point-in-time")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
	assert snapshot.status == "Available", snapshot.status
	assert snapshot.size_bytes > 0, snapshot.size_bytes
	assert_probe(
		server_name, "phase-snapshot-present.sh", SNAPSHOT_ROOTFS_PATH=snapshot.rootfs_path
	)

	# Restore the snapshot back onto the same VM (rollback in place).
	snapshot.restore_to_vm()
	vm.reload()
	assert vm.status == "Stopped", vm.status

	# Boot it and confirm identity is intact (same UUID-derived hostname etc.).
	vm.start()
	vm.reload()
	assert vm.status == "Running", vm.status
	assert_probe(
		server_name,
		"phase5-guest-identity.sh",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=vm.name,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)


def _check_resize(server_name: str, vm) -> None:
	"""Stop -> Resize (grow vCPU/mem/disk) -> assert config -> Start."""
	vm.stop()
	vm.reload()

	vm.resize(vcpus=2, memory_megabytes=1024, disk_gigabytes=6)
	vm.reload()
	assert vm.vcpus == 2 and vm.memory_megabytes == 1024 and vm.disk_gigabytes == 6
	assert_probe(
		server_name,
		"phase-resized-config.sh",
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
	assert_probe(server_name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)


def _check_rebuild_from_image(server_name: str, vm) -> None:
	"""Stop -> Rebuild from the base image -> Start, identity preserved."""
	vm.stop()
	vm.reload()
	vm.rebuild("image")
	vm.reload()
	assert vm.status == "Stopped", vm.status
	vm.start()
	vm.reload()
	assert_probe(
		server_name,
		"phase5-guest-identity.sh",
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
		"phase5-ipv4-egress.sh",
		timeout_seconds=180,
		VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)


def _check_clone(server_name: str, vm):
	"""Snapshot (from Stopped) -> clone into a new VM with fresh identity."""
	vm.stop()
	vm.reload()
	snapshot_name = vm.snapshot("vm-snapshot for clone")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)

	clone_name = snapshot.clone_to_new_vm(
		title="vm-snapshot clone", ssh_public_key=ephemeral_public_key()
	)
	frappe.db.commit()
	clone = frappe.get_doc("Virtual Machine", clone_name)
	assert clone.name != vm.name
	assert clone.ipv6_address != vm.ipv6_address
	assert clone.clone_source_rootfs == snapshot.rootfs_path

	wait_for_vm_running(clone.name, timeout_seconds=120)
	clone.reload()
	# The clone boots with its own fresh identity (new hostname from new UUID,
	# regenerated host keys / machine-id) — the guest-identity probe asserts it.
	assert_probe(
		server_name,
		"phase5-guest-identity.sh",
		timeout_seconds=180,
		VIRTUAL_MACHINE_NAME=clone.name,
		VIRTUAL_MACHINE_IPV6=clone.ipv6_address,
		SSH_PRIVATE_KEY=ephemeral_private_key(),
	)

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
	assert_probe(server_name, "phase-is-paused.sh", VIRTUAL_MACHINE_NAME=vm.name)

	# Pausing again from Paused is rejected by the controller guard.
	with expect_validation_error("cannot pause"):
		vm.pause()

	vm.resume()
	vm.reload()
	assert vm.status == "Running", vm.status
	assert_probe(server_name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)

	# Stop it so the terminate at the end is from a quiet state.
	vm.stop()
	vm.reload()
