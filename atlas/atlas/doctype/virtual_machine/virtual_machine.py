import ipaddress
import uuid

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.networking import (
	CPU_MODE_RELAXED,
	allocate_ipv6,
	cgroup_args,
	derive_ipv4_link,
	derive_mac,
	derive_netns,
	derive_tap,
	derive_uid,
	derive_veth_pair,
	resource_limit_args,
)
from atlas.atlas.placement import apply_user_defaults
from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result

# Never change after insert — identity and the key the rootfs was built with.
IMMUTABLE_AFTER_INSERT = (
	"title",
	"server",
	"image",
	"tenant",
)

# Frozen on ordinary saves (drift protection: the on-host VM must match the
# doc) but mutable through resize() on a Stopped VM, which rewrites the
# firecracker config and grows the disk to match. The resize() path sets
# `flags.resizing` so validate() lets these through.
RESIZE_MUTABLE = (
	"vcpus",
	"cpu_max_cores",
	"cpu_mode",
	"memory_megabytes",
	"disk_gigabytes",
	"data_disk_gigabytes",
)


class VirtualMachine(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_mode: DF.Literal["", "site", "admin"]
		clone_source_data_rootfs: DF.Data | None
		clone_source_rootfs: DF.Data | None
		cpu_max_cores: DF.Float
		cpu_mode: DF.Literal["Hard cap", "Relaxed"]
		data_disk_format_and_mount: DF.Check
		data_disk_gigabytes: DF.Int
		data_disk_mount_point: DF.Data | None
		disk_gigabytes: DF.Int
		has_memory_snapshot: DF.Check
		image: DF.Link
		ipv6_address: DF.Data | None
		is_proxy: DF.Check
		last_started: DF.Datetime | None
		last_stopped: DF.Datetime | None
		mac_address: DF.Data | None
		memory_megabytes: DF.Int
		memory_snapshot_on_stop: DF.Check
		public_ipv4: DF.Data | None
		server: DF.Link
		size_preset: DF.Literal["Custom", "Shared 1x", "Shared 2x", "Shared 4x", "Shared 8x", "Dedicated 1x"]
		ssh_public_key: DF.LongText
		status: DF.Literal["Pending", "Running", "Paused", "Stopped", "Failed", "Terminated"]
		stop_protection: DF.Check
		tap_device: DF.Data | None
		tenant: DF.Link | None
		termination_protection: DF.Check
		title: DF.Data
		vcpus: DF.Int
		warm_snapshot: DF.Link | None
	# end: auto-generated types

	@property
	def ssh_command(self) -> str:
		if self.is_sshpiper:
			return f"ssh -p 222 root@{self.ipv6_address}" if self.ipv6_address else ""
		gateway_ipv4 = frappe.db.get_value(
			"Virtual Machine",
			{
				"server": self.server,
				"is_sshpiper": 1,
				"sshpiper_configured": 1,
				"status": "Running",
				"public_ipv4": ["is", "set"],
			},
			"public_ipv4",
		)
		return f"ssh {self.title}@{gateway_ipv4}" if gateway_ipv4 else ""

	@ssh_command.setter
	def ssh_command(self, _value: object) -> None:
		# Virtual field: ignore writes. Frappe's hydrate path setattrs every
		# field on the doc when loading from the form; the value is derived
		# from ipv6_address.
		pass

	def autoname(self) -> None:
		# autoname() runs from set_new_name(), called by Document.insert()
		# after before_insert(). Dependent fields are derived in
		# before_validate(), which runs after set_new_name.
		self.name = str(uuid.uuid4())

	def before_insert(self) -> None:
		# A dashboard user creates a VM with no server/image; fill them before
		# anything that depends on server (ipv6 allocation derives from it).
		# No-op for the operator path, which supplies both. See placement.py.
		apply_user_defaults(self)
		self.set_build_mode_default()
		self.set_status_default()
		self.set_ipv6_address()

	def after_insert(self) -> None:
		"""Auto-provision: enqueue the provision job so the operator never
		has to click `Provision` on a freshly-created Pending VM.

		enqueue_after_commit so the worker only starts once this insert's
		transaction has committed — otherwise auto_provision can look up the VM
		before the row exists ("Virtual Machine ... not found")."""
		frappe.enqueue(
			"atlas.atlas.doctype.virtual_machine.virtual_machine.auto_provision",
			queue="long",
			timeout=300,
			enqueue_after_commit=True,
			virtual_machine_name=self.name,
		)

	def before_validate(self) -> None:
		if not self.is_new():
			return
		self.set_cpu_defaults()
		self.set_mac_address()
		self.set_tap_device()

	def set_cpu_defaults(self) -> None:
		# cpu_max_cores is the VM's guaranteed CPU bandwidth share; vcpus is the
		# guest thread count. A caller who sets only vcpus (the operator desk path,
		# the bootstrap seed, direct API) wants whole-core bandwidth — default the
		# share to vcpus so those VMs behave exactly as before this field existed.
		# The size presets set it explicitly (fractional shares for sub-1 sizes).
		if not self.cpu_max_cores:
			self.cpu_max_cores = float(self.vcpus or 1)
		# cpu_mode picks how that share is enforced. Default to the relaxed
		# cpu.weight floor + burst ceiling — VMs get their guaranteed share under
		# contention but burst into spare host CPU when it's idle — for any caller
		# that does not opt into the hard-cap model. The JSON default covers the
		# form path; this covers direct API/test construction.
		if not self.cpu_mode:
			self.cpu_mode = CPU_MODE_RELAXED

	def set_build_mode_default(self) -> None:
		"""Inherit the bench bake mode from the base image when the caller didn't set
		one. A promoted bench golden carries build_mode (admin/site); a VM created from
		it via the ordinary `image` field should map its FQDN the same way the golden was
		baked, without the caller having to restate the mode. Only fills an unset value,
		so the recipe-stamped build VM (image_build) and snapshot clones — which set
		build_mode explicitly — are untouched, and an ordinary base image (no mode) leaves
		it empty (→ site, the harmless default everywhere it is read). See spec/08."""
		if self.build_mode or not self.image:
			return
		self.build_mode = frappe.db.get_value("Virtual Machine Image", self.image, "build_mode") or None

	def set_status_default(self) -> None:
		if not self.status:
			self.status = "Pending"

	def set_ipv6_address(self) -> None:
		if not self.ipv6_address:
			self.ipv6_address = allocate_ipv6(self.server)

	def set_mac_address(self) -> None:
		if not self.mac_address:
			self.mac_address = derive_mac(self.name)

	def set_tap_device(self) -> None:
		if not self.tap_device:
			self.tap_device = derive_tap(self.name)

	def validate(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		guarded = IMMUTABLE_AFTER_INSERT
		if not self.flags.resizing:
			# Outside resize(), the resource fields are frozen too.
			guarded = guarded + RESIZE_MUTABLE
		for field in guarded:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def configure_sshpiper(self) -> str:
		"""Inject this ingress VM's gateway credentials and start SSHPiper."""
		from atlas.atlas.sshpiper import configure_gateway

		return configure_gateway(self.name)

	@frappe.whitelist()
	def provision(self) -> str:
		if self.status not in ("Pending", "Failed"):
			frappe.throw(f"Cannot provision from {self.status}")
		task = run_task(
			server=self.server,
			script="provision-vm",
			variables=self._provision_variables(),
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def start(self) -> str:
		"""Start a Stopped VM. When the last stop captured a memory snapshot
		(has_memory_snapshot), the host resumes the guest from it in milliseconds
		instead of cold-booting; the start Task is the same either way — the
		launcher and the unit's vm-restore.py hook decide from the on-host marker.
		The snapshot is consumed by the start (restored or not), so the flag
		clears here unconditionally."""
		if self.status != "Stopped":
			frappe.throw(f"Cannot start from {self.status}")
		task = run_task(
			server=self.server,
			script="start-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.has_memory_snapshot = 0
		self.last_started = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def stop(self, memory_snapshot: bool | None = None) -> str:
		"""Stop a Running/Paused VM. The default is the plain unit stop. With
		`memory_snapshot` (default: the VM's memory_snapshot_on_stop flag, off
		unless the operator opted in), the stop Task first captures the guest's
		full memory state so the next Start resumes it in milliseconds; on any
		snapshot failure the Task falls back to the plain stop on its own — the
		VM always ends up Stopped, only the next Start's speed differs.
		has_memory_snapshot records which way it went."""
		# A Paused VM's unit is still active (vCPUs frozen, not shut down), so
		# `systemctl stop` is the correct full shutdown from either state.
		if self.status not in ("Running", "Paused"):
			frappe.throw(f"Cannot stop from {self.status}")
		if self.stop_protection:
			frappe.throw(_("Disable stop protection before stopping this VM"))
		if memory_snapshot is None:
			memory_snapshot = bool(self.memory_snapshot_on_stop)
		# frm.call / REST send a JSON/stringy value; normalize to bool.
		memory_snapshot = memory_snapshot in (True, 1, "1", "true", "True", "yes")
		snapshotted = False
		if memory_snapshot:
			# The memory dump is RAM-sized; give it disk-write time, not the
			# 30s a plain systemctl stop needs.
			task = run_task(
				server=self.server,
				script="snapshot-stop-vm",
				variables={
					"VIRTUAL_MACHINE_NAME": self.name,
					"ATLAS_FC_UID": str(derive_uid(self.name)),
				},
				virtual_machine=self.name,
				timeout_seconds=120,
			)
			snapshotted = bool(parse_result(task.stdout)["memory_snapshot"])
		else:
			task = run_task(
				server=self.server,
				script="stop-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.name},
				virtual_machine=self.name,
				timeout_seconds=30,
			)
		self.status = "Stopped"
		self.has_memory_snapshot = 1 if snapshotted else 0
		self.last_stopped = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def restart(self, cold: bool = False) -> dict:
		"""Stop (if Running) then Start. Two Tasks. A Paused VM must resume or
		stop first — restart is deliberately Running/Stopped only.

		When the VM opted into memory_snapshot_on_stop, a restart is a
		state-preserving POWER CYCLE: the stop captures the guest's memory and
		the start resumes it — milliseconds, but the guest never reboots, so a
		wedged guest stays wedged. Pass `cold=True` for a true reboot (plain
		stop, full cold boot). Without the opt-in, restart is the plain
		stop + cold boot it always was."""
		if self.status not in ("Running", "Stopped"):
			frappe.throw(f"Cannot restart from {self.status}")
		cold = cold in (True, 1, "1", "true", "True", "yes")
		stop_task = self.stop(memory_snapshot=False if cold else None) if self.status == "Running" else None
		start_task = self.start()
		return {"stop_task": stop_task, "start_task": start_task}

	@frappe.whitelist()
	def pause(self) -> str:
		"""Freeze a Running VM's vCPUs via Firecracker's API socket. RAM stays
		resident (unlike Stop, which is a full shutdown). Reversible with
		resume()."""
		if self.status != "Running":
			frappe.throw(f"Cannot pause from {self.status}")
		task = run_task(
			server=self.server,
			script="pause-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Paused"
		self.save()
		return task.name

	@frappe.whitelist()
	def resume(self) -> str:
		"""Unfreeze a Paused VM's vCPUs via the API socket."""
		if self.status != "Paused":
			frappe.throw(f"Cannot resume from {self.status}")
		task = run_task(
			server=self.server,
			script="resume-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def snapshot(self, title: str | None = None, live: bool = False) -> str:
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
		if live:
			if self.status not in ("Running", "Paused"):
				frappe.throw(
					f"Live snapshot needs a Running or Paused VM (status is {self.status}); "
					f"for a Stopped VM take a normal snapshot"
				)
		elif self.status != "Stopped":
			frappe.throw(
				f"Stop the VM before snapshotting (status is {self.status}), "
				f"or pass live=True for a crash-consistent live snapshot"
			)
		title = (title or "").strip() or self._default_snapshot_title()
		# A snapshot captures BOTH disks: the data disk is a first-class peer of
		# root. We record its size + mount config on the row so a clone/restore can
		# reconstruct the data disk faithfully even if the source VM later changes.
		has_data = bool(self.data_disk_gigabytes)
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": title,
				"virtual_machine": self.name,
				"server": self.server,
				"status": "Pending",
				"source_image": self.image,
				"disk_gigabytes": self.disk_gigabytes,
				"data_disk_gigabytes": self.data_disk_gigabytes,
				"data_disk_mount_point": self.data_disk_mount_point,
				"data_disk_format_and_mount": self.data_disk_format_and_mount,
				# Carry the bench bake mode so a clone of this golden maps its FQDN to
				# the baked site (site) or the admin console (admin) — empty for an
				# ordinary VM snapshot (spec/08).
				"build_mode": self.build_mode or None,
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
			"VIRTUAL_MACHINE_NAME": self.name,
			"SNAPSHOT_ROOTFS_PATH": rootfs_path,
		}
		if data_rootfs_path:
			variables["DATA_SNAPSHOT_ROOTFS_PATH"] = data_rootfs_path
		task = run_task(
			server=self.server,
			script="snapshot-vm",
			variables=variables,
			virtual_machine=self.name,
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

	def _default_snapshot_title(self) -> str:
		"""`<vm title> — <YYYY-MM-DD HH:mm>` for an unnamed snapshot."""
		stamp = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M")
		return f"{self.title} — {stamp}"

	@frappe.whitelist()
	def capture_warm_snapshot(self, title: str | None = None) -> str:
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
		if self.status not in ("Running", "Paused"):
			frappe.throw(
				f"A warm snapshot needs a Running or Paused VM (status is {self.status}); "
				f"for a Stopped VM take a plain snapshot"
			)
		title = (title or "").strip() or self._default_snapshot_title()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": title,
				"virtual_machine": self.name,
				"server": self.server,
				"status": "Pending",
				"kind": "Warm",
				"source_image": self.image,
				"disk_gigabytes": self.disk_gigabytes,
				# Carry the bench bake mode (empty for an ordinary VM) so a clone of a
				# golden maps its FQDN correctly on first boot (spec/08).
				"build_mode": self.build_mode or None,
				# The frozen vmstate pins the machine and its tap name; a warm clone
				# must reproduce all three exactly (clone_to_new_vm enforces it).
				"vcpus": self.vcpus,
				"memory_megabytes": self.memory_megabytes,
				"tap_device": self.tap_device,
			}
		).insert(ignore_permissions=True)
		rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
		memory_directory = f"/var/lib/atlas/snapshots/{snapshot.name}"
		task = run_task(
			server=self.server,
			script="warm-snapshot-vm",
			variables={
				"VIRTUAL_MACHINE_NAME": self.name,
				"ATLAS_FC_UID": str(derive_uid(self.name)),
				"SNAPSHOT_ROOTFS_PATH": rootfs_path,
				"MEMORY_DIRECTORY": memory_directory,
			},
			virtual_machine=self.name,
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

	@frappe.whitelist()
	def rebuild(self, source_type: str, source: str | None = None) -> str:
		"""Replace this Stopped VM's disk while keeping its identity.

		`source_type` is "snapshot" (restore one of this VM's own snapshots)
		or "image" (lay down a fresh rootfs from a base image; `source`
		defaults to the VM's current image). Name, IPv6, MAC, tap and SSH key
		are unchanged — only the disk bytes are swapped. The VM stays Stopped;
		the operator starts it when ready."""
		if self.status != "Stopped":
			frappe.throw(f"Stop the VM before rebuilding (status is {self.status})")
		variables = self._rebuild_variables(source_type, source)
		task = run_task(
			server=self.server,
			script="rebuild-vm",
			variables=variables,
			virtual_machine=self.name,
			timeout_seconds=300,
		)
		# rebuild-vm.py dropped any pending memory snapshot (saved RAM must never
		# be restored over a replaced disk); mirror that on the row.
		self.db_set("has_memory_snapshot", 0)
		return task.name

	def _rebuild_variables(self, source_type: str, source: str | None) -> dict:
		# Rebuild rewrites the guest's network env, so it must re-inject the
		# NAT44 v4 link or the rebuilt guest would boot with no v4 egress.
		#
		# An attached Reserved IP needs NOTHING here: rebuild swaps only the disk
		# and does not touch the host-side network.env, so its RESERVED_IPV4 line
		# (written by vm-reserved-ip.py at attach) survives the rebuild and the
		# 1:1-NAT is re-applied by vm-network-up.py on the next unit start. The
		# guest never sees the reserved IP either way (it binds only its /30).
		base = {
			"VIRTUAL_MACHINE_NAME": self.name,
			"DISK_GB": str(self.disk_gigabytes),
			"VIRTUAL_MACHINE_IPV6": self.ipv6_address,
			"SSH_PUBLIC_KEY": self.ssh_public_key,
			"ATLAS_FC_UID": str(derive_uid(self.name)),
			**self._ipv4_link_variables(),
			# Data-disk config so the rebuilt rootfs regains its fstab mount line.
			# DATA_DISK_MOUNT_AT is the one consumed on a rebuild-from-image (data
			# disk preserved); a restore also gets DATA_SNAPSHOT_ROOTFS_PATH below.
			**self._data_disk_variables(),
		}
		if source_type == "snapshot":
			if not source:
				frappe.throw(_("Rebuild from snapshot requires a snapshot"))
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source)
			if snapshot.virtual_machine != self.name:
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
			image_name = source or self.image
			image = frappe.get_doc("Virtual Machine Image", image_name)
			return {
				**base,
				"IMAGE_NAME": image.image_name,
				"ROOTFS_FILENAME": image.rootfs_filename,
			}
		frappe.throw(f"Unknown rebuild source_type: {source_type!r}")

	@frappe.whitelist()
	def resize(
		self,
		vcpus: int | None = None,
		cpu_max_cores: float | None = None,
		cpu_mode: str | None = None,
		memory_megabytes: int | None = None,
		disk_gigabytes: int | None = None,
		data_disk_gigabytes: int | None = None,
	) -> str:
		"""Change vCPU / CPU bandwidth / memory / disk on a Stopped VM.

		Firecracker can't resize a running VM (machine-config is pre-boot
		only), so the operator stops first. Disk may only grow — ext4 shrink
		is unsafe and the on-host rootfs is already that large. The new values
		are persisted, then resize-vm.py rewrites the firecracker config and
		grows the rootfs to match. The VM stays Stopped.

		`cpu_max_cores` is the VM's guaranteed CPU bandwidth share and `cpu_mode`
		is how it is enforced (hard cgroup cpu.max ceiling vs. cpu.weight floor +
		burst). resize-vm.py rewrites firecracker.json (vcpu_count/mem) and grows
		the disk, but does NOT regenerate the per-VM jailer launcher — so a new
		share, mode, or burst ceiling takes effect on the next re-provision, not on
		the next Start (the same pre-existing behavior the whole-core cpu.max cap
		already has). We still persist the new values so the doc stays the source
		of truth and capacity accounting is correct. When the caller changes vcpus
		but leaves cpu_max_cores unset, keep the share in step for a whole-core VM
		(share == old vcpus); otherwise the explicit share (or the unchanged
		fractional one) stands. cpu_mode is left untouched unless passed."""
		if self.status != "Stopped":
			frappe.throw(f"Stop the VM before resizing (status is {self.status})")
		new_vcpus = int(vcpus) if vcpus else self.vcpus
		new_memory = int(memory_megabytes) if memory_megabytes else self.memory_megabytes
		new_disk = int(disk_gigabytes) if disk_gigabytes else self.disk_gigabytes
		new_data_disk = int(data_disk_gigabytes) if data_disk_gigabytes else self.data_disk_gigabytes
		new_cpu_max = self._resolve_resize_cpu_max(cpu_max_cores, new_vcpus)
		new_cpu_mode = cpu_mode or self.cpu_mode
		if new_disk < self.disk_gigabytes:
			frappe.throw(f"Disk can only grow: {self.disk_gigabytes} GB → {new_disk} GB is a shrink")
		# The data disk grows like the root disk, with one extra rule: resize only
		# GROWS an existing data disk. Adding one to a VM that never had one would
		# also need a new Firecracker drive + fstab line (a re-provision concern),
		# so that path is recreate-the-VM, not resize.
		if new_data_disk != self.data_disk_gigabytes:
			if not self.data_disk_gigabytes:
				# fmt: off
				frappe.throw(_("This VM has no data disk; recreate the VM to add one (resize only grows an existing data disk)"))
				# fmt: on
			if new_data_disk < self.data_disk_gigabytes:
				frappe.throw(
					f"Data disk can only grow: {self.data_disk_gigabytes} GB → {new_data_disk} GB is a shrink"
				)
		# Run the on-host resize first; run_task raises on failure, so we only
		# persist the new values once the config and disk actually changed.
		# Saving before the Task would let a failed resize-vm.py leave the doc
		# claiming a size the host never applied — the exact drift the freeze
		# guards against.
		variables = {
			"VIRTUAL_MACHINE_NAME": self.name,
			"VCPUS": str(new_vcpus),
			"MEMORY_MB": str(new_memory),
			"DISK_GB": str(new_disk),
		}
		if new_data_disk:
			variables["DATA_DISK_GB"] = str(new_data_disk)
			variables["DATA_DISK_FORMAT"] = "1" if self.data_disk_format_and_mount else "0"
		task = run_task(
			server=self.server,
			script="resize-vm",
			variables=variables,
			virtual_machine=self.name,
			timeout_seconds=120,
		)
		self.vcpus = new_vcpus
		self.cpu_max_cores = new_cpu_max
		self.cpu_mode = new_cpu_mode
		self.memory_megabytes = new_memory
		self.disk_gigabytes = new_disk
		self.data_disk_gigabytes = new_data_disk
		# resize-vm.py dropped any pending memory snapshot (the saved vmstate no
		# longer matches the new machine config); mirror that on the row.
		self.has_memory_snapshot = 0
		self.flags.resizing = True
		self.save()
		return task.name

	def _resolve_resize_cpu_max(self, cpu_max_cores: float | None, new_vcpus: int) -> float:
		"""The cpu_max_cores to persist on a resize.

		An explicit value wins. Otherwise, when the VM was whole-core (cap ==
		current vcpus) and the resize changes vcpus, track the new vcpus so a
		whole-core VM stays whole-core. A fractional VM (cap != vcpus) keeps its
		cap untouched unless the caller passes a new one."""
		if cpu_max_cores:
			return float(cpu_max_cores)
		if self.cpu_max_cores == float(self.vcpus):
			return float(new_vcpus)
		return float(self.cpu_max_cores)

	@frappe.whitelist()
	def regenerate_host_keys(self) -> str:
		"""Rotate this VM's SSH host keys (change its SSH identity) on a **Stopped**
		VM. Stopped-only because the host mounts the rootfs to rewrite the keys.

		This is the explicit, opt-in counterpart to the preserve-by-default rule:
		provision establishes host keys at birth and rebuild/restore PRESERVE them
		(so a rollback never breaks clients' known_hosts), so changing them is a
		deliberate action. After the next Start the VM presents new host keys and
		clients must refresh known_hosts — that is the intended effect."""
		if self.status != "Stopped":
			frappe.throw(f"Stop the VM before regenerating host keys (status is {self.status})")
		task = run_task(
			server=self.server,
			script="regenerate-host-keys-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=60,
		)
		# The script dropped any pending memory snapshot (the rootfs changed
		# under it); mirror that on the row.
		self.db_set("has_memory_snapshot", 0)
		return task.name

	@frappe.whitelist()
	def terminate(self) -> str:
		if self.status == "Terminated":
			frappe.throw(_("VM is already terminated"))
		if self.termination_protection:
			frappe.throw(_("Disable termination protection before terminating this VM"))
		task = run_task(
			server=self.server,
			script="terminate-vm",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=60,
		)
		self.status = "Terminated"
		self.save()
		self._detach_reserved_ip()
		self._revoke_tunnels()
		self._delete_subdomains()
		self._delete_custom_domains()
		self._delete_snapshots()
		return task.name

	def _revoke_tunnels(self) -> None:
		"""Revoke every VPN Tunnel to this VM on terminate (spec/19-vpn-broker.md).
		terminate-vm.py tears down the VM's netns/veth but the tunnel's wg interface
		lives in the host ROOT netns and survives that, so each tunnel's revoke()
		runs the host down Task to remove it. Idempotent: a VM with no tunnels is a
		no-op; already-Revoked tunnels are skipped."""
		for name in frappe.get_all(
			"VPN Tunnel",
			filters={"virtual_machine": self.name, "status": ["!=", "Revoked"]},
			pluck="name",
		):
			frappe.get_doc("VPN Tunnel", name).revoke()

	def _detach_reserved_ip(self) -> None:
		"""Release the VM's attached public IPv4 (if any) back to its Server's
		pool on terminate, so the address can be re-attached to another VM. The
		Reserved IP row survives — only the attachment is cleared."""
		for name in frappe.get_all("Reserved IP", filters={"virtual_machine": self.name}, pluck="name"):
			frappe.get_doc("Reserved IP", name).detach()

	def _delete_subdomains(self) -> None:
		"""Drop every Subdomain that routes to this VM, so terminating it stops routing
		(each row's on_trash deconverges the regional proxy fleet). The leak fix
		(spec/18 Component F): today ONLY `Site.terminate` cleans up Subdomains, so a VM
		terminated directly — by the operator, or any non-`Site` path (a bench VM,
		`Site.terminate`'s own backing-VM teardown after it already cleared its one
		Subdomain) — would otherwise strand its routes on a /128 that `allocate_ipv6`
		re-hands to the next tenant, a cross-tenant traffic leak.

		A `Subdomain` is the LINKER (its `virtual_machine` field points AT this VM), so
		deleting it is unobstructed by Frappe's link-integrity guard (which protects the
		linked-TO doc) — unlike `Site._delete_subdomain`, which first clears the Site's
		own Link field to the Subdomain. Idempotent: a VM with no Subdomains is a no-op.
		`terminate()` is the ONLY controller-side teardown — there is deliberately NO
		scheduled sweeper backstop (spec/18 Component F, "Why no sweeper"): because this
		deletes a VM's rows in the same teardown that releases its /128, a row never
		outlives its VM's address, so the case a sweeper would catch is closed here."""
		for name in frappe.get_all("Subdomain", filters={"virtual_machine": self.name}, pluck="name"):
			frappe.delete_doc("Subdomain", name, ignore_permissions=True)

	def _delete_custom_domains(self) -> None:
		"""Drop every Custom Domain that routes to this VM, so terminating it stops routing
		(each row's on_trash deconverges the regional proxy fleet's custom-domain map). The
		full-FQDN sibling of `_delete_subdomains` (spec/18 Phase 2): a custom domain is the
		LINKER (its `virtual_machine` points AT this VM), so deletion is unobstructed by the
		link-integrity guard. Idempotent: a VM with no Custom Domains is a no-op. Like the
		Subdomain teardown, this is part of the SAME teardown that releases the VM's /128, so
		a custom-domain route never outlives its VM's address (Component F)."""
		for name in frappe.get_all("Custom Domain", filters={"virtual_machine": self.name}, pluck="name"):
			frappe.delete_doc("Custom Domain", name, ignore_permissions=True)

	def _delete_snapshots(self) -> None:
		"""Drop this VM's snapshot rows after terminate. Each row's on_trash
		lvremoves its snapshot LV — snapshot LVs live in the thin pool, OUTSIDE
		the VM directory terminate-vm.py rm -rf'd, so they survive that and must
		be removed via the per-snapshot delete path (one SSH round trip each;
		the script is idempotent).

		The golden bench snapshot is the exception: it is a DURABLE artifact that
		outlives its build VM — every self-serve site clones from it. Terminating the
		build VM (the bake leaves it as scratch) must NOT take the golden with it, or
		the snapshot row stays "Available" while its LV is gone and the next clone
		fails late in provision-vm.py ("snapshot LV not found"). So skip the snapshot
		currently referenced by Atlas Settings.default_bench_snapshot — and every
		Available WARM snapshot, the same durable-artifact contract: a warm golden is
		the per-server fan-out source and outlives its build VM by design (its own
		on_trash removes the LV + memory pair when the operator retires it)."""
		golden = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
		for row in frappe.get_all(
			"Virtual Machine Snapshot",
			filters={"virtual_machine": self.name},
			fields=["name", "kind", "status"],
		):
			if row.name == golden:
				continue
			if row.kind == "Warm" and row.status == "Available":
				continue
			# force=1: a bake's snapshot is linked from its Image Build row, and
			# delete_doc runs on_trash (host artifact removal, non-transactional)
			# BEFORE the link check — a plain delete would destroy the artifacts
			# and then abort on the link, stranding the row. The Image Build keeps
			# a dangling audit link instead.
			frappe.delete_doc("Virtual Machine Snapshot", row.name, ignore_permissions=True, force=1)

	def _ipv4_link_variables(self) -> dict:
		"""The per-VM NAT44 egress link, derived from the v6 address — no
		stored field. The guest gets a private v4 + default route; the host
		masquerades it (see scripts/vm-network-up.py, spec/06-networking.md).
		Shared by provision (clone too) and rebuild, which both re-inject the
		guest network env."""
		host_cidr, guest_cidr = derive_ipv4_link(self.ipv6_address)
		return {
			"IPV4_HOST_CIDR": host_cidr,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
		}

	def _data_disk_variables(self) -> dict:
		"""The data-disk Task vars, shared by provision/rebuild/resize. Empty when
		the VM has no data disk (DATA_DISK_GB unset → the script's `0` default → no
		data disk created). DATA_DISK_FORMAT is "1"/"0" (an int flag, not a bool —
		the Task runner would render a bool as a truthy string); DATA_DISK_MOUNT_AT
		is empty when format-and-mount is off, so the script skips the fstab line."""
		if not self.data_disk_gigabytes:
			return {}
		return {
			"DATA_DISK_GB": str(self.data_disk_gigabytes),
			"DATA_DISK_FORMAT": "1" if self.data_disk_format_and_mount else "0",
			"DATA_DISK_MOUNT_AT": self.data_disk_mount_point if self.data_disk_format_and_mount else "",
		}

	def _provision_variables(self) -> dict:
		image = frappe.get_doc("Virtual Machine Image", self.image)
		host_veth, namespace_veth = derive_veth_pair(self.name)
		variables = {
			"VIRTUAL_MACHINE_NAME": self.name,
			"IMAGE_NAME": self.image,
			"KERNEL_FILENAME": image.kernel_filename,
			"ROOTFS_FILENAME": image.rootfs_filename,
			"VCPUS": str(self.vcpus),
			"MEMORY_MB": str(self.memory_megabytes),
			"DISK_GB": str(self.disk_gigabytes),
			"MAC_ADDRESS": self.mac_address,
			"TAP_DEVICE": self.tap_device,
			"VIRTUAL_MACHINE_IPV6": self.ipv6_address,
			"SSH_PUBLIC_KEY": self.ssh_public_key,
			# Jail isolation parameters. All derived from the VM's own UUID and
			# resource fields, so the on-host jail is reconstructible from the
			# row. provision-vm.py bakes these into the per-VM jailer-launch.sh
			# (exec'd by the systemd unit) and writes network.env (read by
			# vm-network-up.py) from them.
			"ATLAS_FC_UID": str(derive_uid(self.name)),
			"ATLAS_NETNS": derive_netns(self.name),
			"HOST_VETH": host_veth,
			"NAMESPACE_VETH": namespace_veth,
			# cgroup/resource LIMITS as values-only lists. The runner renders each
			# as a repeatable CLI flag (--cgroup-arg <value>); provision-vm.py
			# prefixes each with --cgroup / --resource-limit when it builds the
			# per-VM launcher. A value with an internal space (cpu.max's "<quota>
			# <period>") is one argv token end to end — no systemd word-splitting,
			# so the shell's newline-join + mapfile workaround is gone.
			"CGROUP_ARG": _cgroup_values(
				cgroup_args(
					self.cpu_max_cores,
					self.memory_megabytes,
					self.disk_gigabytes,
					self.cpu_mode,
					self.vcpus,
				)
			),
			"RESOURCE_ARG": _cgroup_values(resource_limit_args(self.disk_gigabytes)),
			# Per-VM NAT44 v4 egress link (host/guest /30 + gateway).
			**self._ipv4_link_variables(),
			# An attached Reserved IP (if any) so a fresh provision re-creates its
			# inbound 1:1-NAT on first boot. Empty/None is dropped by the Task
			# runner's flag rendering, leaving the env clean for ordinary VMs.
			"RESERVED_IPV4": self.public_ipv4,
			# The Atlas controller base URL written into the guest at
			# /etc/atlas-routing.env — the trusted-edge FQDN a bench VM's in-guest routing
			# client POSTs the register/deregister/check_label/list endpoints to (spec/18).
			# NON-SECRET — uniform on every VM, like the MMDS device: a non-bench VM's guest
			# client simply has no choke point that calls it. Empty (no request context,
			# e.g. a bare `bench execute`) is dropped by the Task runner, leaving the env
			# clean.
			"ROUTING_BASE_URL": _routing_base_url(),
		}
		# Clone: seed the disk from a snapshot's rootfs instead of the pristine
		# image. The kernel still comes from the image; provision-vm.py's image
		# probe (step 0) stays meaningful. Identity is re-derived from this VM's
		# own UUID, so the clone never shares host keys / machine-id with its
		# source.
		if self.clone_source_rootfs:
			variables["SNAPSHOT_ROOTFS_PATH"] = self.clone_source_rootfs
		# Warm clone: provision-vm.py additionally stages the golden memory pair
		# behind a READY marker and this VM's identity as MMDS metadata, and the
		# disk stays a byte-exact CoW (no grow/inject — the frozen RAM must keep
		# matching it). The tap NAME already flows above: clone_to_new_vm pinned
		# self.tap_device to the golden's (the vmstate binds the tap by name).
		if self.warm_snapshot:
			variables["WARM_SNAPSHOT_DIRECTORY"] = frappe.db.get_value(
				"Virtual Machine Snapshot", self.warm_snapshot, "memory_directory"
			)
		# Data disk (the root disk's peer): size + format/mount config, plus —
		# when cloning — the data-disk snapshot to seed it from, so the clone's
		# /home comes up with the source's data.
		variables.update(self._data_disk_variables())
		if self.clone_source_data_rootfs:
			variables["DATA_SNAPSHOT_ROOTFS_PATH"] = self.clone_source_data_rootfs
		return variables


def _routing_base_url() -> str:
	"""The Atlas controller base URL a guest's routing client POSTs to (spec/18).

	`frappe.utils.get_url()` resolves the public site URL (honoring `host_name` /
	the request host behind the proxy). Returns "" if it can't be resolved (no
	configured host_name and no request context — e.g. a bare worker job before
	host_name is set), which the Task runner drops, leaving /etc/atlas-routing.env
	unwritten and the guest client a clean no-op. NON-SECRET, so there is no harm in
	injecting it broadly."""
	try:
		return frappe.utils.get_url() or ""
	except Exception:
		return ""


def _cgroup_values(interleaved: list[str]) -> list[str]:
	"""Drop the flag tokens from networking.cgroup_args/resource_limit_args,
	which interleave `["--cgroup", "<value>", "--cgroup", "<value>"]`. The
	provision task wants values only — it owns the --cgroup / --resource-limit
	prefix when it builds the per-VM launcher — so keep every token that is not
	itself a flag (does not start with '--')."""
	return [token for token in interleaved if not token.startswith("--")]


def auto_provision(virtual_machine_name: str) -> None:
	"""Background-job entrypoint. Called by `after_insert` so the operator
	doesn't have to click Provision. No-op if the VM has moved past Pending
	(operator intervened, manual provision raced us, etc.)."""
	virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine_name)
	if virtual_machine.status != "Pending":
		return
	virtual_machine.provision()
