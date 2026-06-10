"""LVM thin-pool, object-oriented — the successor to scripts/lib/lvm.sh.

Every per-VM disk is a thin LV that is a CoW snapshot of a read-only base image
LV; the pool sits on a loopback PV (a sparse backing file on the root fs). The
two objects here are the only place that knows the pool layout.

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

import os
import stat
from dataclasses import dataclass

from atlas._run import run, run_ok


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
		return run_ok("sudo", "lvs", "--noheadings", self._ref)

	@property
	def is_protected(self) -> bool:
		return self.name == self._pool.pool_name or self.name.startswith(self._PROTECTED_PREFIXES)

	@property
	def size_bytes(self) -> int:
		"""The device's byte count — typed int, not a stdout line to grep."""
		return int(run("sudo", "blockdev", "--getsize64", self.device_path).strip())

	@property
	def device_number(self) -> DeviceNumber:
		return DeviceNumber.from_lsblk(run("lsblk", "-ndo", "MAJ:MIN", self.device_path))

	def activate(self) -> "LogicalVolume":
		"""Activate with -K (so activation-skip-flagged snapshots come up), wait
		for udev, fall back to vgmknodes. Returns self once the node is a block
		device, else raises. Chainable: `pool.snapshot(...).activate()`."""
		run("sudo", "lvchange", "-ay", "-K", self._ref)
		self._wait_for_node()
		return self

	def snapshot_into(self, new: "LogicalVolume") -> "LogicalVolume":
		"""Create `new` as a thin CoW snapshot of self and activate it. Instant,
		O(1). Idempotent — re-activates if `new` already exists. self may itself
		be a snapshot (clone path). Returns `new`."""
		if new.exists:
			return new.activate()
		# No -L/--thinpool: snapshotting a thin LV inherits its pool and size.
		run("sudo", "lvcreate", "-s", self._ref, "-n", new.name)
		return new.activate()

	def expose_in_jail(self, jail_node: str, uid: int) -> None:
		"""Expose this LV's block device inside a jailer chroot at jail_node,
		owned by uid (gid == uid), mode 0660. On rebuild the dev_t can change, so
		always remove and re-create (idempotent). Device access is pure DAC."""
		number = self.device_number
		run("sudo", "rm", "-f", jail_node)
		run("sudo", "mknod", jail_node, "b", str(number.major), str(number.minor))
		run("sudo", "chown", f"{uid}:{uid}", jail_node)
		run("sudo", "chmod", "0660", jail_node)

	def remove(self) -> None:
		"""Remove this LV. No-op if already gone. Refuses protected LVs so
		teardown can never destroy shared state even if handed a wrong name."""
		if self.is_protected:
			raise ProtectedVolumeError(f"refusing to remove protected LV {self.name!r}")
		if self.exists:
			run("sudo", "lvremove", "-f", self._ref)

	# --- private: the raw host pokes ---

	@property
	def _ref(self) -> str:
		"""`<vg>/<name>`, the form lvm CLI tools take."""
		return f"{self._pool.volume_group}/{self.name}"

	def _wait_for_node(self) -> None:
		run("sudo", "udevadm", "settle")
		if not self._node_is_block_device():
			run("sudo", "vgmknodes", self._pool.volume_group)
			run("sudo", "udevadm", "settle")
		if not self._node_is_block_device():
			raise RuntimeError(f"LV {self.name} activated but {self.device_path} is not a block device")

	def _node_is_block_device(self) -> bool:
		try:
			return stat.S_ISBLK(os.stat(self.device_path).st_mode)
		except (FileNotFoundError, PermissionError):
			return False


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
		"""The sparse loopback backing file the pool's PV sits on."""
		return f"{self.pool_directory}/atlas-pool.img"

	@property
	def _pool_lv(self) -> LogicalVolume:
		return LogicalVolume(self.pool_name, self)

	def ensure(self) -> None:
		"""Idempotently bring up the thin pool: sparse backing file, loop device,
		PV/VG, thin pool LV. Re-running is a no-op once the pool exists (re-binds
		the loop device, which a reboot drops, then re-activates the VG with -K so
		skip-flagged VM disk snapshots come back). The port of atlas_pool_ensure;
		the ONLY line that changes for a real attached block device is the
		loop_device assignment.

		The double existence-check (top + before lvcreate) guards a reboot race:
		LVM's own event autoactivation can surface pool0 between the two checks,
		and a bare lvcreate would then abort 'already exists' (exit 5). With the
		inner gate a concurrently-activated pool just falls through to vgchange.
		"""
		if self._pool_lv.exists:
			if not run("sudo", "losetup", "-j", self.backing_image).strip():
				run("sudo", "losetup", "--find", self.backing_image)
			run("sudo", "vgchange", "-ay", "-K", self.volume_group, quiet=True)
			return

		run("sudo", "install", "-d", "-m", "0700", self.pool_directory)
		if not run_ok("test", "-f", self.backing_image):
			run("sudo", "truncate", "-s", self.data_size, self.backing_image)

		loop_device = run("sudo", "losetup", "-j", self.backing_image).strip()
		loop_device = loop_device.split(":", 1)[0] if loop_device else ""
		if not loop_device:
			loop_device = run("sudo", "losetup", "--find", "--show", self.backing_image).strip()

		if not run_ok("sudo", "pvs", loop_device):
			run("sudo", "pvcreate", loop_device, quiet=True)
		if not run_ok("sudo", "vgs", self.volume_group):
			run("sudo", "vgcreate", self.volume_group, loop_device, quiet=True)

		if not self._pool_lv.exists:
			run(
				"sudo",
				"lvcreate",
				"--type",
				"thin-pool",
				"--name",
				self.pool_name,
				"--poolmetadatasize",
				self.metadata_size,
				"--extents",
				"100%FREE",
				self.volume_group,
				quiet=True,
			)
		run("sudo", "vgchange", "-ay", "-K", self.volume_group, quiet=True)

	def create_thin(self, lv: LogicalVolume, disk_gigabytes: int) -> LogicalVolume:
		"""Create `lv` as a blank thin volume of disk_gigabytes (its bytes private
		to it — `-V`, no origin), then activate it. Idempotent: activate-and-return
		if it already exists, so a re-provision reuses the same disk. This is how a
		fresh data disk is born (the `import_base_image` shape minus the dd +
		read-only flip — a data disk is writable and empty)."""
		if lv.exists:
			return lv.activate()
		run(
			"sudo",
			"lvcreate",
			"--type",
			"thin",
			"--thinpool",
			self.pool_name,
			"-V",
			f"{disk_gigabytes}G",
			"-n",
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
			"sudo",
			"lvcreate",
			"--type",
			"thin",
			"--thinpool",
			self.pool_name,
			"-V",
			f"{disk_gigabytes}G",
			"-n",
			lv.name,
			self.volume_group,
			quiet=True,
		)
		lv.activate()
		try:
			run(
				"sudo",
				"dd",
				f"if={source_file}",
				f"of={lv.device_path}",
				"bs=4M",
				"conv=fsync",
				"status=none",
			)
		except Exception:
			run("sudo", "lvremove", "-f", f"{self.volume_group}/{lv.name}", check=False, quiet=True)
			raise
		# Read-only at the LVM layer: the base is never mounted writable, so a
		# stray write can't corrupt the shared origin. Per-VM snapshots are
		# independently writable regardless.
		run("sudo", "lvchange", "--permission", "r", f"{self.volume_group}/{lv.name}")
		return lv

	@property
	def usage(self) -> PoolUsage:
		"""Current data/metadata fill, read from the pool LV."""
		ref = f"{self.volume_group}/{self.pool_name}"
		return PoolUsage.from_lvs(
			run("sudo", "lvs", "--noheadings", "-o", "data_percent", ref),
			run("sudo", "lvs", "--noheadings", "-o", "metadata_percent", ref),
		)
