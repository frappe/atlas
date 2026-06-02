import frappe
from frappe.model.document import Document

from atlas.atlas.ssh import run_task


class VirtualMachineSnapshot(Document):
	@frappe.whitelist()
	def clone_to_new_vm(
		self,
		title: str,
		ssh_public_key: str,
		vcpus: int | None = None,
		memory_megabytes: int | None = None,
		disk_gigabytes: int | None = None,
	) -> str:
		"""Create a NEW Virtual Machine whose disk is seeded from this snapshot.

		The clone is a fresh VM: new UUID, new IPv6, new MAC, new SSH host keys
		and machine-id (all re-derived at provision from the new UUID). It is a
		disk template, not a live-state resume — the safe path that avoids the
		duplicate-identity hazard Firecracker warns about. Disk defaults to the
		snapshot's size (the rootfs is already grown to it); a smaller value is
		rejected because the filesystem can't shrink to fit."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		source_vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		disk = int(disk_gigabytes) if disk_gigabytes else self.disk_gigabytes
		if disk < self.disk_gigabytes:
			frappe.throw(
				f"Clone disk ({disk} GB) cannot be smaller than the snapshot ({self.disk_gigabytes} GB)"
			)
		clone = frappe.get_doc({
			"doctype": "Virtual Machine",
			"title": title,
			"server": source_vm.server,
			"image": self.source_image,
			"vcpus": int(vcpus) if vcpus else source_vm.vcpus,
			"memory_megabytes": int(memory_megabytes) if memory_megabytes else source_vm.memory_megabytes,
			"disk_gigabytes": disk,
			"ssh_public_key": ssh_public_key,
			"clone_source_rootfs": self.rootfs_path,
		}).insert(ignore_permissions=True)
		return clone.name

	@frappe.whitelist()
	def restore_to_vm(self) -> str:
		"""Restore this snapshot onto its own VM (rollback in place). Thin
		wrapper around Virtual Machine.rebuild so the Stopped-state guard and
		the Task all live in one place. Returns the Task name."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		virtual_machine = frappe.get_doc("Virtual Machine", self.virtual_machine)
		return virtual_machine.rebuild("snapshot", self.name)

	def on_trash(self) -> None:
		"""Remove the on-host snapshot LV when the row is deleted.

		The snapshot LV is the only thing this row points at; once the row is
		gone the LV is dead weight. We remove it in the same gesture so the pool
		doesn't accumulate orphans. Idempotent script — a missing LV is a no-op.

		Unlike the old file-backed snapshots (which lived under the VM directory
		and were swept by terminate-vm.py's `rm -rf`), a snapshot LV lives in the
		thin pool, OUTSIDE the VM directory — so it survives terminate's directory
		removal and MUST be lvremoved here even when terminate() cascades the row
		deletions of a Terminated VM. (No Terminated short-circuit: that would
		leak the snapshot LV.)"""
		if not self.server or not self.rootfs_path:
			return
		if not frappe.db.exists("Server", self.server):
			return
		run_task(
			server=self.server,
			script="delete-snapshot-vm.py",
			variables={"SNAPSHOT_ROOTFS_PATH": self.rootfs_path},
			virtual_machine=self.virtual_machine,
			timeout_seconds=60,
		)
