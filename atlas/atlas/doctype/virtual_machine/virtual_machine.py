import ipaddress
import uuid

import frappe
from frappe.model.document import Document

from atlas.atlas.networking import (
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

# Never change after insert — identity and the key the rootfs was built with.
IMMUTABLE_AFTER_INSERT = (
	"title",
	"server",
	"image",
	"ssh_public_key",
)

# Frozen on ordinary saves (drift protection: the on-host VM must match the
# doc) but mutable through resize() on a Stopped VM, which rewrites the
# firecracker config and grows the disk to match. The resize() path sets
# `flags.resizing` so validate() lets these through.
RESIZE_MUTABLE = (
	"vcpus",
	"memory_megabytes",
	"disk_gigabytes",
)


class VirtualMachine(Document):
	@property
	def ssh_command(self) -> str:
		if not self.ipv6_address:
			return ""
		return f"ssh root@{self.ipv6_address}"

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
		self.set_status_default()
		self.set_ipv6_address()

	def after_insert(self) -> None:
		"""Auto-provision: enqueue the provision job so the operator never
		has to click `Provision` on a freshly-created Pending VM."""
		frappe.enqueue(
			"atlas.atlas.doctype.virtual_machine.virtual_machine.auto_provision",
			queue="long",
			timeout=300,
			virtual_machine_name=self.name,
		)

	def before_validate(self) -> None:
		if not self.is_new():
			return
		self.set_mac_address()
		self.set_tap_device()

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
	def provision(self) -> str:
		if self.status not in ("Pending", "Failed"):
			frappe.throw(f"Cannot provision from {self.status}")
		task = run_task(
			server=self.server,
			script="provision-vm.sh",
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
		if self.status != "Stopped":
			frappe.throw(f"Cannot start from {self.status}")
		task = run_task(
			server=self.server,
			script="start-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def stop(self) -> str:
		# A Paused VM's unit is still active (vCPUs frozen, not shut down), so
		# `systemctl stop` is the correct full shutdown from either state.
		if self.status not in ("Running", "Paused"):
			frappe.throw(f"Cannot stop from {self.status}")
		task = run_task(
			server=self.server,
			script="stop-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Stopped"
		self.last_stopped = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def restart(self) -> dict:
		"""Stop (if Running) then Start. Two Tasks. A Paused VM must resume or
		stop first — restart is deliberately Running/Stopped only."""
		if self.status not in ("Running", "Stopped"):
			frappe.throw(f"Cannot restart from {self.status}")
		stop_task = self.stop() if self.status == "Running" else None
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
			script="pause-vm.sh",
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
			script="resume-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save()
		return task.name

	@frappe.whitelist()
	def snapshot(self, title: str) -> str:
		"""Copy the rootfs of this Stopped VM into a new Virtual Machine
		Snapshot row. Returns the snapshot's name.

		Stopped-only because copying a mounted/live ext4 risks a torn
		filesystem; a cleanly unmounted rootfs copies consistently. The
		operator stops first (the form offers the prompt)."""
		if self.status != "Stopped":
			frappe.throw(f"Stop the VM before snapshotting (status is {self.status})")
		snapshot = frappe.get_doc({
			"doctype": "Virtual Machine Snapshot",
			"title": title,
			"virtual_machine": self.name,
			"server": self.server,
			"status": "Pending",
			"source_image": self.image,
			"disk_gigabytes": self.disk_gigabytes,
		}).insert(ignore_permissions=True)
		# The snapshot is an LVM thin snapshot, not a file copy. rootfs_path holds
		# its LV device path (derived from the snapshot's UUID, like the VM disk
		# LV) — no schema change, and it flows unchanged into restore/clone, which
		# read the LV name back from this path.
		rootfs_path = f"/dev/atlas/atlas-snap-{snapshot.name}"
		task = run_task(
			server=self.server,
			script="snapshot-vm.sh",
			variables={
				"VIRTUAL_MACHINE_NAME": self.name,
				"SNAPSHOT_ROOTFS_PATH": rootfs_path,
			},
			virtual_machine=self.name,
			timeout_seconds=300,
		)
		# One atomic update: the Task already succeeded and the on-host file
		# exists, so the row must end up Available. Folding the three writes into
		# a single db_set means there's no window where rootfs_path/size_bytes
		# landed but status didn't (a half-update that stranded the row in
		# Pending). size_bytes is a Long Int / bigint column — a real multi-GB
		# rootfs overflows a plain Int.
		snapshot.db_set({
			"rootfs_path": rootfs_path,
			"size_bytes": _parse_size_bytes(task.stdout),
			"status": "Available",
		})
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
			script="rebuild-vm.sh",
			variables=variables,
			virtual_machine=self.name,
			timeout_seconds=300,
		)
		return task.name

	def _rebuild_variables(self, source_type: str, source: str | None) -> dict:
		# Rebuild rewrites the guest's network env, so it must re-inject the
		# NAT44 v4 link or the rebuilt guest would boot with no v4 egress.
		base = {
			"VIRTUAL_MACHINE_NAME": self.name,
			"DISK_GB": str(self.disk_gigabytes),
			"VIRTUAL_MACHINE_IPV6": self.ipv6_address,
			"SSH_PUBLIC_KEY": self.ssh_public_key,
			"ATLAS_FC_UID": str(derive_uid(self.name)),
			**self._ipv4_link_variables(),
		}
		if source_type == "snapshot":
			if not source:
				frappe.throw("Rebuild from snapshot requires a snapshot")
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source)
			if snapshot.virtual_machine != self.name:
				frappe.throw("Snapshot belongs to a different Virtual Machine")
			if snapshot.status != "Available":
				frappe.throw(f"Snapshot is not Available (status is {snapshot.status})")
			return {**base, "SNAPSHOT_ROOTFS_PATH": snapshot.rootfs_path}
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
		memory_megabytes: int | None = None,
		disk_gigabytes: int | None = None,
	) -> str:
		"""Change vCPU / memory / disk on a Stopped VM.

		Firecracker can't resize a running VM (machine-config is pre-boot
		only), so the operator stops first. Disk may only grow — ext4 shrink
		is unsafe and the on-host rootfs is already that large. The new values
		are persisted, then resize-vm.sh rewrites the firecracker config and
		grows the rootfs to match. The VM stays Stopped."""
		if self.status != "Stopped":
			frappe.throw(f"Stop the VM before resizing (status is {self.status})")
		new_vcpus = int(vcpus) if vcpus else self.vcpus
		new_memory = int(memory_megabytes) if memory_megabytes else self.memory_megabytes
		new_disk = int(disk_gigabytes) if disk_gigabytes else self.disk_gigabytes
		if new_disk < self.disk_gigabytes:
			frappe.throw(
				f"Disk can only grow: {self.disk_gigabytes} GB → {new_disk} GB is a shrink"
			)
		# Run the on-host resize first; run_task raises on failure, so we only
		# persist the new values once the config and disk actually changed.
		# Saving before the Task would let a failed resize-vm.sh leave the doc
		# claiming a size the host never applied — the exact drift the freeze
		# guards against.
		task = run_task(
			server=self.server,
			script="resize-vm.sh",
			variables={
				"VIRTUAL_MACHINE_NAME": self.name,
				"VCPUS": str(new_vcpus),
				"MEMORY_MB": str(new_memory),
				"DISK_GB": str(new_disk),
			},
			virtual_machine=self.name,
			timeout_seconds=120,
		)
		self.vcpus = new_vcpus
		self.memory_megabytes = new_memory
		self.disk_gigabytes = new_disk
		self.flags.resizing = True
		self.save()
		return task.name

	@frappe.whitelist()
	def terminate(self) -> str:
		if self.status == "Terminated":
			frappe.throw("VM is already terminated")
		task = run_task(
			server=self.server,
			script="terminate-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=60,
		)
		self.status = "Terminated"
		self.save()
		self._delete_snapshots()
		return task.name

	def _delete_snapshots(self) -> None:
		"""Drop this VM's snapshot rows after terminate. Each row's on_trash
		lvremoves its snapshot LV — snapshot LVs live in the thin pool, OUTSIDE
		the VM directory terminate-vm.sh rm -rf'd, so they survive that and must
		be removed via the per-snapshot delete path (one SSH round trip each;
		the script is idempotent)."""
		for name in frappe.get_all(
			"Virtual Machine Snapshot", filters={"virtual_machine": self.name}, pluck="name"
		):
			frappe.delete_doc("Virtual Machine Snapshot", name, ignore_permissions=True)

	def _ipv4_link_variables(self) -> dict:
		"""The per-VM NAT44 egress link, derived from the v6 address — no
		stored field. The guest gets a private v4 + default route; the host
		masquerades it (see scripts/vm-network-up.sh, spec/06-networking.md).
		Shared by provision (clone too) and rebuild, which both re-inject the
		guest network env."""
		host_cidr, guest_cidr = derive_ipv4_link(self.ipv6_address)
		return {
			"IPV4_HOST_CIDR": host_cidr,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
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
			# row. provision-vm.sh bakes these into the per-VM jailer-launch.sh
			# (exec'd by the systemd unit) and writes network.env (read by
			# vm-network-up.sh) from them.
			"ATLAS_FC_UID": str(derive_uid(self.name)),
			"ATLAS_NETNS": derive_netns(self.name),
			"HOST_VETH": host_veth,
			"NAMESPACE_VETH": namespace_veth,
			# Newline-joined (one argv token per line), NOT space-joined: the
			# jailer's `--cgroup cpu.max=<quota> <period>` value carries an
			# internal space, so a space-joined string fed through systemd's
			# whitespace-splitting ExecStart would shatter "100000 100000" into
			# a stray positional the jailer rejects. provision-vm.sh rebuilds
			# the exact argv with `mapfile` into the per-VM launcher.
			"ATLAS_CGROUP_ARGS": "\n".join(
				cgroup_args(self.vcpus, self.memory_megabytes, self.disk_gigabytes)
			),
			"ATLAS_RESOURCE_ARGS": "\n".join(resource_limit_args(self.disk_gigabytes)),
			# Per-VM NAT44 v4 egress link (host/guest /30 + gateway).
			**self._ipv4_link_variables(),
		}
		# Clone: seed the disk from a snapshot's rootfs instead of the pristine
		# image. The kernel still comes from the image; provision-vm.sh's image
		# probe (step 0) stays meaningful. Identity is re-derived from this VM's
		# own UUID, so the clone never shares host keys / machine-id with its
		# source.
		if self.clone_source_rootfs:
			variables["SNAPSHOT_ROOTFS_PATH"] = self.clone_source_rootfs
		return variables


def _parse_size_bytes(stdout: str) -> int:
	"""Pull the snapshot byte count from snapshot-vm.sh stdout.

	The script prints a `SIZE_BYTES=<n>` line; the rest is `bash -x` trace
	noise. Absent (older script, truncated output) → 0 rather than raise:
	the size is informational, not load-bearing."""
	for line in (stdout or "").splitlines():
		if line.startswith("SIZE_BYTES="):
			try:
				return int(line.split("=", 1)[1].strip())
			except ValueError:
				return 0
	return 0


def auto_provision(virtual_machine_name: str) -> None:
	"""Background-job entrypoint. Called by `after_insert` so the operator
	doesn't have to click Provision. No-op if the VM has moved past Pending
	(operator intervened, manual provision raced us, etc.)."""
	virtual_machine = frappe.get_doc("Virtual Machine", virtual_machine_name)
	if virtual_machine.status != "Pending":
		return
	virtual_machine.provision()

