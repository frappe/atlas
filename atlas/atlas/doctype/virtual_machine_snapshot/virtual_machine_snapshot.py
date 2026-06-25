import re

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result

# An image name becomes both a Frappe doc name (autoname field:image_name) and an
# LVM LV name (atlas-image-<name>). LVM LV names allow [a-zA-Z0-9+_.-]; we are
# stricter — lowercase alnum plus dot/dash — so the name is also a clean docname
# and a clean DNS-ish label. Reject anything else loudly rather than minting an LV
# the host's lvcreate would refuse or a docname Frappe would mangle.
_IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


class VirtualMachineSnapshot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_mode: DF.Literal["", "site", "admin"]
		data_disk_format_and_mount: DF.Check
		data_disk_gigabytes: DF.Int
		data_disk_mount_point: DF.Data | None
		data_rootfs_path: DF.Data | None
		disk_gigabytes: DF.Int
		host_signature: DF.SmallText | None
		kind: DF.Literal["Cold", "Warm"]
		memory_directory: DF.Data | None
		memory_megabytes: DF.Int
		rootfs_path: DF.Data | None
		server: DF.Link | None
		source_image: DF.Link | None
		status: DF.Literal["Pending", "Available", "Failed"]
		tap_device: DF.Data | None
		tenant: DF.Link | None
		title: DF.Data
		vcpus: DF.Int
		virtual_machine: DF.Link
	# end: auto-generated types

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
				# Carry the bench bake mode onto the clone, where its first-boot
				# deploy reads it (site → rename the baked site to the FQDN; admin →
				# map the FQDN to the admin console). Empty for a plain snapshot.
				"build_mode": self.build_mode or None,
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
				# Carry the bench bake mode onto the warm clone (a warm v16 golden is
				# site mode), so its first-boot deploy maps the FQDN correctly.
				"build_mode": self.build_mode or None,
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

	@frappe.whitelist()
	def promote_to_image(self, image_name: str, title: str | None = None) -> str:
		"""Promote this cold snapshot into a first-class same-server base image, so
		new VMs provision from it via the ordinary `image` field instead of locating
		a one-off snapshot to clone (spec/08-images.md, spec/15-image-builder.md).

		On `self.server`, `promote-snapshot-image.py` dd's the snapshot LV into a
		new read-only `atlas-image-<image_name>` LV; then we register a *local*
		(URL-less) `Virtual Machine Image` row pointing at it. The kernel is free —
		the row reuses the snapshot's `source_image` kernel (already on the server),
		so only the rootfs LV is new and nothing leaves the host.

		Warm snapshots are rejected up front: a warm snapshot's value is its frozen
		memory pair (clones RESUME it), and a base image's contract is the opposite —
		clones cold-boot and provision *requires* grow + tune2fs + identity injection,
		none of which the memory pair survives. Promoting a warm snapshot could only
		mean discarding that pair, throwing away the one thing that distinguishes it
		from an ordinary bake. So we throw on `kind == "Warm"`; promote a cold
		snapshot, clone the warm one with `clone_to_new_vm`. (Operator decision,
		2026-06-19.)

		**Promote is root-only.** A snapshot captures both the root and data disks,
		and `clone_to_new_vm` carries the data disk into a clone — but a base image is
		a *root* template (the `Virtual Machine Image` DocType has no data-disk
		fields), so a promoted image would silently drop the snapshot's data disk.
		Rather than lose data quietly, we throw on a data-disk snapshot: promote a
		data-less snapshot, or clone this one to preserve its data disk.

		**Ordering: the image row is the durable anchor.** We insert the row FIRST,
		then run the host `dd`. The host import is idempotent, so a host failure
		(which raises and rolls the row back with it) leaves nothing to clean —
		never an orphaned read-only `atlas-image-*` LV with no owning row (those are
		protected, so the lifecycle could never reclaim one). This mirrors
		`Virtual Machine.snapshot()`'s insert-then-host-work order.

		Returns the new image's name."""
		if self.status != "Available":
			frappe.throw(f"Snapshot is not Available (status is {self.status})")
		if self.kind == "Warm":
			frappe.throw(
				_(
					"A warm snapshot cannot be promoted to an image — its value is the frozen memory pair clones resume, which a cold-booting base image discards. Promote a cold snapshot, or clone this one with Clone to new VM."
				)
			)
		if self.data_disk_gigabytes:
			frappe.throw(
				_(
					"This snapshot has a data disk; a base image captures only the root disk (the image has no data-disk fields), so promoting would silently drop it. Clone this snapshot with Clone to new VM to keep the data disk, or promote a data-less snapshot."
				)
			)
		if not self.source_image:
			# The kernel is inherited from source_image; a snapshot with no recorded
			# source image (a malformed row) has no kernel to point the image at.
			frappe.throw(_("Snapshot has no source image to inherit a kernel from; cannot promote."))

		image_name = (image_name or "").strip().lower()
		if not _IMAGE_NAME_RE.match(image_name):
			frappe.throw(
				f"Image name {image_name!r} is invalid — use lowercase letters, digits, "
				"dots and dashes (it becomes both the image record name and the LVM LV name)."
			)
		if frappe.db.exists("Virtual Machine Image", image_name):
			frappe.throw(f"A Virtual Machine Image named {image_name!r} already exists.")

		source_kernel_filename = frappe.db.get_value(
			"Virtual Machine Image", self.source_image, "kernel_filename"
		)
		if not source_kernel_filename:
			frappe.throw(
				f"Source image {self.source_image} has no kernel_filename; cannot promote "
				"(the promoted image reuses its kernel)."
			)

		rootfs_filename = f"atlas-image-{image_name}"
		# Register the local image row FIRST — the durable anchor (see docstring).
		# Empty kernel_url/rootfs_url => a URL-less image (validate permits it;
		# after_insert/sync skip it — its bytes are the promoted LV, already on the
		# server, not a download). rootfs_filename is the LV name; the on-disk file is
		# a presence sentinel the host materializes (provision reads the LV).
		image = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": image_name,
				"title": (title or "").strip() or self.title,
				"kernel_url": "",
				"kernel_filename": source_kernel_filename,
				"kernel_sha256": "",
				"rootfs_url": "",
				"rootfs_filename": rootfs_filename,
				"rootfs_sha256": "",
				"default_disk_gigabytes": self.disk_gigabytes,
				# Carry the bench bake mode onto the base image, so a VM created from it
				# via the ordinary `image` field inherits build_mode and its first-boot
				# deploy maps the FQDN to the admin console (admin) or the baked site
				# (site) — the snapshot→clone path already carried it; this is the
				# promote→image path's equivalent (spec/08). Empty for a non-bench image.
				"build_mode": self.build_mode or None,
				"tenant": self.tenant,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)

		# Then dd the snapshot LV into the read-only atlas-image-<name> LV on the
		# server, and materialize the image dir (kernel hard-linked from the source
		# image, rootfs presence sentinel) so a new VM provisions from it exactly like
		# a synced image. Idempotent on the host (a no-op if the target LV already
		# exists). A host failure raises here and rolls back the image row above with
		# it, so promote is all-or-nothing — never a half-state.
		task = run_task(
			server=self.server,
			script="promote-snapshot-image.py",
			variables={
				"SNAPSHOT_ROOTFS_PATH": self.rootfs_path,
				"IMAGE_NAME": image_name,
				"DISK_GIGABYTES": str(self.disk_gigabytes),
				"ROOTFS_FILENAME": rootfs_filename,
				"SOURCE_IMAGE": self.source_image,
				"KERNEL_FILENAME": source_kernel_filename,
			},
			virtual_machine=self.virtual_machine,
			timeout_seconds=600,
		)
		parse_result(task.stdout)  # fail loud if the script produced no ATLAS_RESULT line
		return image.name

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
