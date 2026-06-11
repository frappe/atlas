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
		cpu_max_cores: float | None = None,
		memory_megabytes: int | None = None,
		disk_gigabytes: int | None = None,
	) -> str:
		"""Create a NEW Virtual Machine whose disk is seeded from this snapshot.

		The clone is a fresh VM: new UUID, new IPv6, new MAC, new SSH host keys
		and machine-id (all re-derived at provision from the new UUID). It is a
		disk template, not a live-state resume — the safe path that avoids the
		duplicate-identity hazard Firecracker warns about. Disk defaults to the
		snapshot's size (the rootfs is already grown to it); a smaller value is
		rejected because the filesystem can't shrink to fit.

		The snapshot is a DURABLE artifact that outlives its build VM (self-serve
		sites clone from the golden indefinitely; the bake leaves the build VM as
		scratch and terminates it). So `server` comes from the snapshot's own row,
		not the source VM — and the source VM is consulted only as a fallback for
		the resource sizing a caller didn't pass. If the build VM is gone AND the
		caller passed no sizing, we fail loud with a clear message rather than
		`DoesNotExistError` deep in get_doc. The self-serve caller always passes
		an explicit size, so it never depends on the build VM surviving."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		if self.kind == "Warm":
			return self._clone_warm(
				title, ssh_public_key, vcpus, cpu_max_cores, memory_megabytes, disk_gigabytes
			)
		disk = int(disk_gigabytes) if disk_gigabytes else self.disk_gigabytes
		if disk < self.disk_gigabytes:
			frappe.throw(
				f"Clone disk ({disk} GB) cannot be smaller than the snapshot ({self.disk_gigabytes} GB)"
			)
		# Source VM is a sizing fallback only — it may have been terminated and its
		# row deleted (bake teardown) long after this durable golden was baked.
		source_vm = (
			frappe.get_doc("Virtual Machine", self.virtual_machine)
			if frappe.db.exists("Virtual Machine", self.virtual_machine)
			else None
		)
		new_vcpus, clone_cpu_max, clone_memory = self._clone_sizing(
			source_vm, vcpus, cpu_max_cores, memory_megabytes
		)
		clone = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": title,
				"server": self.server,
				"image": self.source_image,
				"vcpus": new_vcpus,
				"cpu_max_cores": clone_cpu_max,
				"memory_megabytes": clone_memory,
				"disk_gigabytes": disk,
				"ssh_public_key": ssh_public_key,
				"clone_source_rootfs": self.rootfs_path,
				# The data disk clones too: carry its size + mount config from the
				# snapshot, and seed it from the data-disk snapshot LV (empty when
				# the source had no data disk → a plain image clone with no /vdb).
				"data_disk_gigabytes": self.data_disk_gigabytes,
				"data_disk_format_and_mount": self.data_disk_format_and_mount,
				"data_disk_mount_point": self.data_disk_mount_point,
				"clone_source_data_rootfs": self.data_rootfs_path,
			}
		).insert(ignore_permissions=True)
		return clone.name

	def _clone_warm(
		self,
		title: str,
		ssh_public_key: str,
		vcpus: int | None,
		cpu_max_cores: float | None,
		memory_megabytes: int | None,
		disk_gigabytes: int | None,
	) -> str:
		"""Clone that RESUMES this warm golden instead of booting it.

		The frozen vmstate pins the machine: a warm clone restores at exactly the
		captured vcpus/memory and on a byte-exact CoW of the captured disk (no
		grow — the frozen RAM's filesystem cache must keep matching it), so any
		mismatched override is rejected rather than silently breaking the
		restore. `cpu_max_cores` is free: it is a host-side cgroup cap, invisible
		to the guest. The clone keeps the golden's tap NAME (the vmstate binds
		the tap by name; names are netns-scoped, so N clones don't collide) and
		carries `warm_snapshot` so provision stages the memory pair + MMDS
		identity."""
		if vcpus and int(vcpus) != self.vcpus:
			frappe.throw(f"A warm clone restores at the captured size: vcpus must be {self.vcpus}")
		if memory_megabytes and int(memory_megabytes) != self.memory_megabytes:
			frappe.throw(
				f"A warm clone restores at the captured size: memory must be {self.memory_megabytes} MB"
			)
		if disk_gigabytes and int(disk_gigabytes) != self.disk_gigabytes:
			frappe.throw(
				f"A warm clone's disk cannot be resized: disk must be {self.disk_gigabytes} GB "
				"(the frozen memory state matches that exact disk)"
			)
		clone = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": title,
				"server": self.server,
				"image": self.source_image,
				"vcpus": self.vcpus,
				"cpu_max_cores": float(cpu_max_cores) if cpu_max_cores else float(self.vcpus),
				"memory_megabytes": self.memory_megabytes,
				"disk_gigabytes": self.disk_gigabytes,
				"ssh_public_key": ssh_public_key,
				"clone_source_rootfs": self.rootfs_path,
				"warm_snapshot": self.name,
				"tap_device": self.tap_device,
			}
		).insert(ignore_permissions=True)
		return clone.name

	def _clone_sizing(
		self,
		source_vm,
		vcpus: int | None,
		cpu_max_cores: float | None,
		memory_megabytes: int | None,
	) -> tuple[int, float, int]:
		"""Resolve (vcpus, cpu_max_cores, memory_megabytes) for a clone.

		Explicit caller args always win. For anything left unset we fall back to
		the source VM's value — but only if that row still exists. A golden whose
		build VM was terminated has no source to inherit from, so a caller that
		passes nothing gets a clear error here instead of a `DoesNotExistError`
		from get_doc on the dangling `virtual_machine` link."""
		new_vcpus = int(vcpus) if vcpus else (source_vm.vcpus if source_vm else None)
		clone_memory = (
			int(memory_megabytes) if memory_megabytes else (source_vm.memory_megabytes if source_vm else None)
		)
		if cpu_max_cores:
			clone_cpu_max = float(cpu_max_cores)
		elif source_vm:
			# Carry the source's cap so a fractional source clones to the same
			# fraction; when vcpus is overridden but the source was whole-core,
			# track the new vcpus (before_validate would otherwise default a
			# missing cap up to vcpus).
			if source_vm.cpu_max_cores == float(source_vm.vcpus):
				clone_cpu_max = float(new_vcpus)
			else:
				clone_cpu_max = float(source_vm.cpu_max_cores)
		else:
			clone_cpu_max = None
		if new_vcpus is None or clone_memory is None or clone_cpu_max is None:
			frappe.throw(
				f"Snapshot {self.name}'s build VM no longer exists — "
				"pass vcpus, cpu_max_cores and memory_megabytes explicitly to clone it."
			)
		return new_vcpus, clone_cpu_max, clone_memory

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
		# Remove both halves of the snapshot: the root snap LV and (when the VM had
		# a data disk) the data snap LV. The empty data path is dropped by the Task
		# runner, so a data-less snapshot's teardown is unchanged. A warm row also
		# owns its durable memory directory (vmstate/mem/host-signature) — same
		# gesture: clone jails only hold hard links, so removing the directory
		# never breaks a clone already provisioned from it.
		run_task(
			server=self.server,
			script="delete-snapshot-vm.py",
			variables={
				"SNAPSHOT_ROOTFS_PATH": self.rootfs_path,
				"DATA_SNAPSHOT_ROOTFS_PATH": self.data_rootfs_path or "",
				"MEMORY_DIRECTORY": self.memory_directory or "",
			},
			virtual_machine=self.virtual_machine,
			timeout_seconds=60,
		)
