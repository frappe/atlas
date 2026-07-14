"""LVM thin-pool, object-oriented — the successor to scripts/lib/lvm.sh.

Every per-VM disk is a thin LV that is a CoW snapshot of a read-only base image
LV; the pool sits on a PV. On a stock cloud droplet with no spare disk the PV is
a loopback file (a sparse backing file on the root fs); on bare metal with real
NVMe (Scaleway Elastic Metal) the PV is the NVMe device(s) themselves — the
backing is chosen by `PoolBacking` (see below). The two objects here are the only
place that knows the pool layout.

- `LogicalVolume` is one LV. It knows its own name and device path, whether it
  exists, how to activate, snapshot, and remove itself. Naming is derived, never
  stored (mirrors networking.py's derive_*): a VM disk is atlas-vm-<uuid>, a
  snapshot atlas-snap-<uuid>, a base image atlas-image-<name>.
- `ThinPool` is the VG + thin pool. It mints LVs by role (`vm_disk`, `snapshot`,
  `base_image`, `from_device`) and answers pool-fullness.

Public methods are the verbs an operator-level task calls (`snapshot`, `remove`,
`activate`). Private methods (leading underscore) are the raw `lvchange` /
`udevadm` / `lsblk` pokes — the host surface that e2e exercises. The pure parsing
that bit us on real hosts (lsblk MAJ:MIN padding, the data_percent decimal) is
isolated in @staticmethods so it is unit-testable with no LVM stack at all.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass

from atlas._run import _substitute, install_directory, install_file, run, run_ok


@dataclass(frozen=True)
class DeviceNumber:
	"""A block device's (major, minor) — typed, so a caller can never transpose
	the two or feed mknod a minor with trailing whitespace (the bug the
	LVM bench-traps memory records)."""

	major: int
	minor: int

	@classmethod
	def from_lsblk(cls, lsblk_output: str) -> "DeviceNumber":
		"""Parse `lsblk -ndo MAJ:MIN` output. The column is right-padded
		("252:5  "); stripping is a tested transform, not an inline `tr -d`."""
		major, _, minor = "".join(lsblk_output.split()).partition(":")
		return cls(int(major), int(minor))


@dataclass(frozen=True)
class PoolUsage:
	"""Thin-pool fill, data and metadata tracked independently (they exhaust
	independently). Parsed from two `lvs -o *_percent` reads."""

	data_percent: float
	metadata_percent: float

	# The documented operator-paged threshold (no autoscaler this slice).
	FULL_THRESHOLD = 90.0

	@staticmethod
	def _parse_percent(value: str) -> float:
		"""`lvs -o data_percent` prints a localized decimal ("87.34"), or blank
		if unknown. Blank → 0.0 (the `${data_pct:-0}` shell default)."""
		value = value.strip()
		return float(value) if value else 0.0

	@classmethod
	def from_lvs(cls, data: str, metadata: str) -> "PoolUsage":
		return cls(cls._parse_percent(data), cls._parse_percent(metadata))

	@property
	def too_full_to_snapshot(self) -> bool:
		"""A thin snapshot is free up front, but every later CoW write allocates
		from the pool; snapshotting an almost-full pool courts a stall."""
		return self.data_percent >= self.FULL_THRESHOLD or self.metadata_percent >= self.FULL_THRESHOLD


class ProtectedVolumeError(RuntimeError):
	"""Raised when teardown is asked to remove the pool or a base image LV — the
	shared state VM/snapshot lifecycle must never destroy."""


class LogicalVolume:
	"""One LV in the atlas VG. Knows its name and device path; can check its own
	existence, activate itself, snapshot itself, and remove itself."""

	_PROTECTED_PREFIXES = ("atlas-image-",)

	def __init__(self, name: str, pool: "ThinPool"):
		self.name = name
		self._pool = pool

	def __repr__(self) -> str:
		return f"LogicalVolume({self.name!r})"

	def __eq__(self, other: object) -> bool:
		return isinstance(other, LogicalVolume) and other.name == self.name

	@property
	def device_path(self) -> str:
		"""Single source of truth for where this LV lives. Callers never
		hand-build /dev paths."""
		return f"/dev/{self._pool.volume_group}/{self.name}"

	@property
	def exists(self) -> bool:
		return run_ok("sudo lvs --noheadings {}", self._ref)

	@property
	def is_protected(self) -> bool:
		return self.name == self._pool.pool_name or self.name.startswith(self._PROTECTED_PREFIXES)

	@property
	def size_bytes(self) -> int:
		"""The device's byte count — typed int, not a stdout line to grep."""
		return int(run("sudo blockdev --getsize64 {}", self.device_path).strip())

	@property
	def device_number(self) -> DeviceNumber:
		return DeviceNumber.from_lsblk(run("lsblk -ndo MAJ:MIN {}", self.device_path))

	def activate(self) -> "LogicalVolume":
		"""Activate with -K (so activation-skip-flagged snapshots come up), wait
		for udev, fall back to vgmknodes. Returns self once the node is a block
		device, else raises. Chainable: `pool.snapshot(...).activate()`."""
		run("sudo lvchange -ay -K {}", self._ref)
		self._wait_for_node()
		return self

	def snapshot_into(self, new: "LogicalVolume") -> "LogicalVolume":
		"""Create `new` as a thin CoW snapshot of self and activate it. Instant,
		O(1). Idempotent — re-activates if `new` already exists. self may itself
		be a snapshot (clone path). Returns `new`."""
		if new.exists:
			return new.activate()
		# No -L/--thinpool: snapshotting a thin LV inherits its pool and size.
		run("sudo lvcreate -s {} -n {}", self._ref, new.name)
		return new.activate()

	def expose_in_jail(self, jail_node: str, uid: int) -> None:
		"""Expose this LV's block device inside a jailer chroot at jail_node,
		owned by uid (gid == uid), mode 0660. On rebuild the dev_t can change, so
		always remove and re-create (idempotent). Device access is pure DAC."""
		expose_device_in_jail(self.device_path, jail_node, uid)

	def remove(self) -> None:
		"""Remove this LV. No-op if already gone. Refuses protected LVs so
		teardown can never destroy shared state even if handed a wrong name."""
		if self.is_protected:
			raise ProtectedVolumeError(f"refusing to remove protected LV {self.name!r}")
		if self.exists:
			run("sudo lvremove -f {}", self._ref)

	# --- private: the raw host pokes ---

	@property
	def _ref(self) -> str:
		"""`<vg>/<name>`, the form lvm CLI tools take."""
		return f"{self._pool.volume_group}/{self.name}"

	def _wait_for_node(self) -> None:
		run("sudo udevadm settle")
		if not self._node_is_block_device():
			run("sudo vgmknodes {}", self._pool.volume_group)
			run("sudo udevadm settle")
		if not self._node_is_block_device():
			raise RuntimeError(f"LV {self.name} activated but {self.device_path} is not a block device")

	def _node_is_block_device(self) -> bool:
		try:
			return stat.S_ISBLK(os.stat(self.device_path).st_mode)
		except (FileNotFoundError, PermissionError):
			return False


def expose_device_in_jail(device_path: str, jail_node: str, uid: int) -> None:
	"""Expose any block device at `device_path` inside a jailer chroot at jail_node,
	owned by uid (gid == uid), mode 0660. The device may be an LVM LV OR a
	device-mapper node (e.g. /dev/mapper/atlas-vm-<uuid>-clone during a
	boot-then-hydrate migration, spec/24 §0) — the mknod machinery is identical, it
	only needs the device's (major, minor). Always remove-and-recreate (idempotent;
	the dev_t can change across a rebuild). Device access is pure DAC."""
	number = DeviceNumber.from_lsblk(run("lsblk -ndo MAJ:MIN {}", device_path))
	run("sudo rm -f {}", jail_node)
	run("sudo mknod {} b {} {}", jail_node, number.major, number.minor)
	run("sudo chown {} {}", f"{uid}:{uid}", jail_node)
	run("sudo chmod 0660 {}", jail_node)


# Minimum size (bytes) for a disk to qualify as pool backing. Filters out a
# bare-metal box's empty removable slots (an SD/card reader shows up as a
# 0-byte `disk`), which would otherwise be picked as "unused" and then fail
# pvcreate with "Device open 8:0 has no path names" (proven on a live Scaleway
# Elastic Metal box: /dev/sda is a 0-byte phantom, the real storage is the two
# NVMe drives already consumed by the root RAID). 1 GiB is far below any real
# pool disk and far above any phantom.
_MIN_POOL_DISK_BYTES = 1 << 30


def discover_pool_disks(lsblk_json: str) -> list[str]:
	"""Pick the block devices that may back the pool PV, parsed from
	`lsblk -J -b -o NAME,TYPE,MOUNTPOINT,FSTYPE,PKNAME,SIZE,RM` output.

	Two shapes qualify, both "a raw, unused block device of usable size":

	- A top-level `disk` that carries NO partitions (children), NO filesystem, and
	  NO mountpoint, AND is a real fixed disk: NOT removable (`rm`) and at least
	  `_MIN_POOL_DISK_BYTES`. This is a box's genuine spare NVMe drive (the OS
	  lives elsewhere) — and exactly what a stock single-disk droplet does NOT
	  have (its only disk is partitioned + mounted as root).
	- A software-RAID array (`type` raid0/1/5/6/10), nested under its member
	  partitions, that is itself unused (no fstype/mountpoint/children). This is
	  the `data` RAID-1 (`/dev/md2`) the Scaleway partitioning schema leaves raw
	  for the pool: boot/root md arrays carry an ext4 fstype + mountpoint and are
	  correctly skipped. An md array appears once under EACH member partition in
	  the lsblk tree, so the recursion dedups by name.

	The size/removable guard is load-bearing on bare metal: a Scaleway Elastic
	Metal box exposes an empty card-reader slot as a 0-byte removable `disk`
	(/dev/sda) that otherwise looks "unused", and feeding it to pvcreate fails
	("Device open 8:0 has no path names. Cannot use /dev/sda: device not found").

	So the same probe yields the data RAID array on a RAID-partitioned box, the
	spare devices on a box with free NVMe, and the empty list on a stock droplet,
	with no per-provider branch. Pure: text in, sorted device-path list out, so it
	unit-tests with fixture JSON and no host.
	"""
	tree = json.loads(lsblk_json) if lsblk_json.strip() else {}
	found: set[str] = set()
	for node in tree.get("blockdevices", []):
		_collect_pool_candidates(node, found)
	return sorted(found)


def _collect_pool_candidates(node: dict, found: set[str]) -> None:
	"""Walk one lsblk node + its children, adding any device that qualifies as
	pool backing to `found`. Recurses so a software-RAID array — which lsblk nests
	UNDER its member partitions, not at the top level — is reachable."""
	node_type = node.get("type")
	# A `disk` with children is partitioned (in use); a RAID array (raid0/1/…)
	# with children already carries a filesystem/LVM. Either way children → skip
	# this node as a candidate, but still descend (an md array hangs off a part).
	is_disk = node_type == "disk"
	is_raid = isinstance(node_type, str) and node_type.startswith("raid")
	if (is_disk or is_raid) and _is_unused_block(node):
		found.add(f"/dev/{node['name']}")
	for child in node.get("children", []):
		_collect_pool_candidates(child, found)


def _is_unused_block(node: dict) -> bool:
	"""True if a disk/RAID node is a raw, unused, real fixed device of usable
	size — no partitions/filesystem/mount, not removable, ≥ the size floor."""
	if node.get("children"):
		return False
	if node.get("fstype") or node.get("mountpoint"):
		return False
	# Removable media (card reader / USB) is never pool backing, and a phantom
	# empty slot reports 0 bytes — reject both. `rm`/`size` come back as bools/
	# ints with `-J -b`; tolerate string forms ("1"/"0") from older lsblk.
	if str(node.get("rm")).lower() in ("1", "true"):
		return False
	size = node.get("size")
	return size is not None and int(size) >= _MIN_POOL_DISK_BYTES


class PoolBacking:
	"""Where the thin pool's PV(s) live — the one thing that differs between a
	stock droplet (no spare disk → a sparse loopback file) and a bare-metal box
	with real NVMe (the disks themselves). `ThinPool.ensure()` delegates the
	PV-bring-up and reboot re-assert here so the pool/VG/LV logic stays identical
	across both. The chosen backing is persisted to a state file at first bring-up
	so a reboot re-asserts the SAME backing without re-probing (a disk reorder or
	a freshly-attached blank disk must not silently re-home the pool).

	Selection order (matches the ATLAS_POOL_* env convention):
	1. `ATLAS_POOL_DEVICE` env — explicit, space/comma-separated device paths
	   (the operator escape hatch and what bootstrap passes when it knows).
	2. The persisted state file — a reboot reuses the first-boot choice.
	3. Auto-detected unused whole disks (NVMe on Elastic Metal) → real-device PV.
	4. None of the above → the loopback file (the stock-droplet default).
	"""

	def __init__(self, pool_directory: str, data_size: str):
		self.pool_directory = pool_directory
		self.data_size = data_size

	@property
	def backing_image(self) -> str:
		"""The sparse loopback backing file used when no real disk is available."""
		return f"{self.pool_directory}/atlas-pool.img"

	@property
	def state_file(self) -> str:
		"""Records the device PVs chosen at first bring-up (one path per line), so
		the reboot re-assert is deterministic. Absent ⇒ loopback backing."""
		return f"{self.pool_directory}/pool-devices"

	@staticmethod
	def _split_devices(value: str) -> list[str]:
		return [token for token in value.replace(",", " ").split() if token]

	def _persisted_devices(self) -> list[str]:
		if not run_ok("test -f {}", self.state_file):
			return []
		return self._split_devices(run("sudo cat {}", self.state_file))

	def select_devices(self) -> list[str]:
		"""The device PVs to back the pool, or [] for the loopback file. Applies
		the selection order above; never returns a device that is gone."""
		explicit = self._split_devices(os.environ.get("ATLAS_POOL_DEVICE", ""))
		if explicit:
			return explicit
		persisted = self._persisted_devices()
		if persisted:
			return persisted
		return discover_pool_disks(run("lsblk -J -b -o NAME,TYPE,MOUNTPOINT,FSTYPE,PKNAME,SIZE,RM"))

	def _persist_devices(self, devices: list[str]) -> None:
		install_directory(self.pool_directory, mode="0700")
		install_file("\n".join(devices) + "\n", self.state_file, mode="0600")

	# --- bring the PV(s) up (creating them on first run); return the PV list ---

	def ensure_devices(self) -> list[str]:
		"""Bring up the backing PV(s) and return the device paths to feed pvcreate
		/ vgcreate. For a real-device backing it settles udev so each disk's /dev
		node + LVM dev cache are ready (a freshly-installed bare-metal box races
		pvcreate against udev — `pvcreate /dev/sda` then fails exit 5 "Device open
		8:0 has no path names / device not found"), persists the list on first use,
		and returns it. For the loopback fallback it creates the sparse file (once),
		(re-)binds the loop device, and returns the single loop node — so a reboot
		re-attaches it."""
		devices = self.select_devices()
		if devices:
			self._settle_devices(devices)
			if not self._persisted_devices():
				self._persist_devices(devices)
			return devices
		return [self._ensure_loop_device()]

	def _settle_devices(self, devices: list[str]) -> None:
		"""Make sure each backing disk's /dev node exists and LVM's device cache
		sees it before pvcreate. `udevadm trigger` re-emits the add events a fresh
		bare-metal boot may not have finished processing; `settle` waits them out.
		Idempotent and cheap; harmless on a device that is already ready."""
		run("sudo udevadm trigger --subsystem-match=block", check=False, quiet=True)
		run("sudo udevadm settle", check=False, quiet=True)

	def register_device(self, device: str) -> None:
		"""Register `device` in LVM's devices file so pvcreate/pvs/vgcreate accept
		it. LVM 2.03+ ships a default-on `system.devices` allowlist (RHEL 9 /
		Ubuntu 24.04+): a freshly-attached bare-metal disk not in it is rejected
		with exit 5 `Device open <maj>:<min> has no path names. Cannot use /dev/sda:
		device not found` — even though the /dev node exists. `lvmdevices --adddev`
		adds it durably (survives reboot, unlike a per-command `--devicesfile`
		bypass). No-op for the loopback device (created locally, already allowed)
		and a no-op `|| true` on older LVM that predates the devices file (the
		subcommand is absent) or when the device is already registered."""
		if device.startswith("/dev/loop"):
			return
		run("sudo lvmdevices --adddev {}", device, check=False)

	def reassert(self) -> None:
		"""Reboot re-assert of the backing, BEFORE vgchange. A real-device PV
		survives a reboot intact (nothing to do); a loopback PV loses its loop
		binding, so re-bind it from the persisted backing file."""
		if self._persisted_devices():
			return
		if run_ok("test -f {}", self.backing_image):
			if not run("sudo losetup -j {}", self.backing_image).strip():
				run("sudo losetup --find {}", self.backing_image)

	def _ensure_loop_device(self) -> str:
		install_directory(self.pool_directory, mode="0700")
		if not run_ok("test -f {}", self.backing_image):
			run("sudo truncate -s {} {}", self.data_size, self.backing_image)
		bound = run("sudo losetup -j {}", self.backing_image).strip()
		loop_device = bound.split(":", 1)[0] if bound else ""
		if not loop_device:
			loop_device = run("sudo losetup --find --show {}", self.backing_image).strip()
		return loop_device


class ThinPool:
	"""The atlas VG + thin pool. Mints LVs by role and answers fullness.

	Defaults match scripts/lib/lvm.sh exactly; env overrides preserved. The
	backing file is sparse, so data_size is an overcommit ceiling, not real disk.
	"""

	def __init__(
		self,
		volume_group: str = "atlas",
		pool_name: str = "pool0",
		pool_directory: str = "/var/lib/atlas/pool",
	):
		self.volume_group = volume_group
		self.pool_name = pool_name
		self.pool_directory = pool_directory
		self.data_size = os.environ.get("ATLAS_POOL_DATA_SIZE", "200G")
		self.metadata_size = os.environ.get("ATLAS_POOL_METADATA_SIZE", "1G")
		self.backing = PoolBacking(pool_directory, self.data_size)

	# --- mint LVs by role. Naming is the single place the scheme lives. ---

	def vm_disk(self, uuid: str) -> LogicalVolume:
		return LogicalVolume(f"atlas-vm-{uuid}", self)

	def data_disk(self, uuid: str) -> LogicalVolume:
		"""The per-VM writable data disk (the root disk's peer). A blank thin
		volume — or a CoW snapshot of a data-disk snapshot on clone/restore —
		exposed as the guest's /dev/vdb."""
		return LogicalVolume(f"atlas-data-{uuid}", self)

	def snapshot(self, uuid: str) -> LogicalVolume:
		return LogicalVolume(f"atlas-snap-{uuid}", self)

	def data_snapshot(self, uuid: str) -> LogicalVolume:
		"""The data-disk half of a Virtual Machine Snapshot (the `atlas-snap-`
		root snapshot's peer). Named off the SAME snapshot UUID so the pair is
		recoverable from the snapshot row's two device paths."""
		return LogicalVolume(f"atlas-datasnap-{uuid}", self)

	def base_image(self, image_name: str) -> LogicalVolume:
		return LogicalVolume(f"atlas-image-{image_name}", self)

	def from_device(self, device_path: str) -> LogicalVolume:
		"""Recover an LV from an /dev/atlas/<name> device path — its basename is
		the LV name. Used to read an origin LV back from a snapshot's stored
		device path (clone/restore pass it as SNAPSHOT_ROOTFS_PATH)."""
		return LogicalVolume(device_path.rsplit("/", 1)[-1], self)

	# --- pool + base-image management (the atlas_pool_ensure / atlas_lv_from_file
	# ports — used by bootstrap and sync-image respectively). ---

	@property
	def backing_image(self) -> str:
		"""The sparse loopback backing file the pool's PV sits on (loopback
		backing only). Delegated to PoolBacking, kept for callers/tests that
		reference the path directly."""
		return self.backing.backing_image

	@property
	def _pool_lv(self) -> LogicalVolume:
		return LogicalVolume(self.pool_name, self)

	def ensure(self) -> None:
		"""Idempotently bring up the thin pool: PV(s), VG, thin pool LV. Re-running
		is a no-op once the pool exists — `PoolBacking.reassert()` re-binds the loop
		device (which a reboot drops; a real-device PV needs nothing), then the VG
		is re-activated with -K so skip-flagged VM disk snapshots come back. The
		port of atlas_pool_ensure; the PV bring-up — loopback file vs. real NVMe
		device(s) — is the one thing that varies, and lives in PoolBacking.

		The double existence-check (top + before lvcreate) guards a reboot race:
		LVM's own event autoactivation can surface pool0 between the two checks,
		and a bare lvcreate would then abort 'already exists' (exit 5). With the
		inner gate a concurrently-activated pool just falls through to vgchange.
		"""
		if self._pool_lv.exists:
			self.backing.reassert()
			run("sudo vgchange -ay -K {}", self.volume_group, quiet=True)
			return

		devices = self.backing.ensure_devices()

		for device in devices:
			self.backing.register_device(device)
			if not run_ok("sudo pvs {}", device):
				# --yes: a freshly-installed bare-metal disk may carry a leftover
				# partition/filesystem signature from the vendor image; accept the
				# wipe non-interactively rather than let pvcreate stall on a prompt.
				run("sudo pvcreate --yes {}", device, quiet=True)
		if not run_ok("sudo vgs {}", self.volume_group):
			args = _substitute(
				" ".join("{}" for _ in [self.volume_group, *devices]), (self.volume_group, *devices)
			)
			run("sudo vgcreate " + args, quiet=True)

		if not self._pool_lv.exists:
			run(
				"sudo lvcreate --type thin-pool --name {} --poolmetadatasize {} --extents 100%FREE {}",
				self.pool_name,
				self.metadata_size,
				self.volume_group,
				quiet=True,
			)
		run("sudo vgchange -ay -K {}", self.volume_group, quiet=True)

	def create_thin(self, lv: LogicalVolume, disk_gigabytes: int) -> LogicalVolume:
		"""Create `lv` as a blank thin volume of disk_gigabytes (its bytes private
		to it — `-V`, no origin), then activate it. Idempotent: activate-and-return
		if it already exists, so a re-provision reuses the same disk. This is how a
		fresh data disk is born (the `import_base_image` shape minus the dd +
		read-only flip — a data disk is writable and empty)."""
		if lv.exists:
			return lv.activate()
		run(
			"sudo lvcreate --type thin --thinpool {} -V {} -n {} {}",
			self.pool_name,
			f"{disk_gigabytes}G",
			lv.name,
			self.volume_group,
			quiet=True,
		)
		return lv.activate()

	def import_base_image(self, source_file: str, image_name: str, disk_gigabytes: int) -> LogicalVolume:
		"""Create the read-only base image LV from a pristine ext4 FILE: a thin
		volume of disk_gigabytes, dd the file into it, mark it read-only. This is
		how an image ext4 file becomes the base LV every per-VM disk snapshots
		from. Idempotent — no-op if the LV already exists (a re-synced image keeps
		its base LV). The port of atlas_lv_from_file.

		Created with -V (a thin volume, not a snapshot), so its bytes are private
		to the base — the base has no origin and can never be orphaned. On a
		mid-build failure (dd) the half-populated writable LV is removed.
		"""
		lv = self.base_image(image_name)
		if lv.exists:
			return lv
		run(
			"sudo lvcreate --type thin --thinpool {} -V {} -n {} {}",
			self.pool_name,
			f"{disk_gigabytes}G",
			lv.name,
			self.volume_group,
			quiet=True,
		)
		lv.activate()
		try:
			run(
				"sudo dd if={} of={} bs=4M conv=fsync status=none",
				source_file,
				lv.device_path,
			)
		except Exception:
			run("sudo lvremove -f {}", f"{self.volume_group}/{lv.name}", check=False, quiet=True)
			raise
		# Read-only at the LVM layer: the base is never mounted writable, so a
		# stray write can't corrupt the shared origin. Per-VM snapshots are
		# independently writable regardless.
		run("sudo lvchange --permission r {}", f"{self.volume_group}/{lv.name}")
		return lv

	def import_base_image_from_lv(
		self, source: LogicalVolume, image_name: str, disk_gigabytes: int
	) -> LogicalVolume:
		"""Promote a snapshot LV into a read-only base image LV — the same shape as
		import_base_image, but the source is a LOCAL LV device, not a downloaded
		ext4 file. This is how a baked `Virtual Machine Snapshot` becomes a
		first-class base image new VMs select with the ordinary `image` field
		(spec/08-images.md, spec/15-image-builder.md): a thin volume of
		disk_gigabytes, dd the snapshot's bytes into it, mark it read-only.

		Created with -V (a thin volume, not a snapshot), so the promoted base has no
		origin and can never be orphaned — it outlives the snapshot it was dd'd from
		(deleting the snapshot LV leaves the image untouched, exactly like a base
		image dd'd from a file). Idempotent — no-op if the LV already exists. On a
		mid-build failure (dd) the half-populated writable LV is removed.

		Pre-flight: the source LV must exist and be activated, so `dd` reads a live
		block device rather than a missing/skip-flagged node."""
		lv = self.base_image(image_name)
		if lv.exists:
			return lv
		if not source.exists:
			raise FileNotFoundError(f"source LV {source.name} not found; cannot promote to {lv.name}")
		source.activate()
		run(
			"sudo lvcreate --type thin --thinpool {} -V {} -n {} {}",
			self.pool_name,
			f"{disk_gigabytes}G",
			lv.name,
			self.volume_group,
			quiet=True,
		)
		lv.activate()
		try:
			run(
				"sudo dd if={} of={} bs=4M conv=fsync status=none",
				source.device_path,
				lv.device_path,
			)
		except Exception:
			run("sudo lvremove -f {}", f"{self.volume_group}/{lv.name}", check=False, quiet=True)
			raise
		run("sudo lvchange --permission r {}", f"{self.volume_group}/{lv.name}")
		return lv

	@property
	def usage(self) -> PoolUsage:
		"""Current data/metadata fill, read from the pool LV."""
		ref = f"{self.volume_group}/{self.pool_name}"
		return PoolUsage.from_lvs(
			run("sudo lvs --noheadings -o data_percent {}", ref),
			run("sudo lvs --noheadings -o metadata_percent {}", ref),
		)

	@property
	def size_bytes(self) -> int:
		"""The thin pool's data capacity in bytes (`lvs -o lv_size` on the pool LV).

		For a loopback-backed pool this is the sparse overcommit ceiling
		(`ATLAS_POOL_DATA_SIZE`); for a real-device PV it is the physical disk the
		pool was carved from. This is the disk budget capacity accounting packs the
		VMs' reserved disk against — not the host's raw root filesystem."""
		ref = f"{self.volume_group}/{self.pool_name}"
		return int(run("sudo lvs --noheadings --nosuffix --units b -o lv_size {}", ref).strip())
