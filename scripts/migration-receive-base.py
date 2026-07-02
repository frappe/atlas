#!/usr/bin/env python3
# Target side of a VM migration (spec/24), LOCAL-BASE-IMAGE receive: pull a local
# (snapshot-promoted, un-syncable) base image from the source over NBD so this host
# gains the base rootfs LV + image directory it needs to migrate — and boot — a VM
# on that image. Pairs with migration-export-base.py on the source.
#
# WHY dm-clone (not a plain dd): the base copy is multi-GB, and spec/24 requires the
# migration's progress to be observable at all points. dm-clone exposes a hydrated/
# total region count via `dmsetup status`, so migration-poll-hydration's existing
# percent parse works unchanged — the controller polls it per tick and shows a bar.
# The base is read-only, so we never boot off the read-through device (that's the VM
# disk's job); we just hydrate to 100% then collapse to a plain LV.
#
# Two-phase, driven per scheduler tick by the controller (mirrors clone-target):
#   PHASE=prepare  - nbd-client to the source base + meta exports; create a writable
#                    thin LV; build the dm-clone; extract the image-dir tar (kernel +
#                    sentinel). Idempotent — skips any artifact that already exists.
#   PHASE=finalize - guard hydration == 100%, collapse the dm-clone to the plain LV,
#                    mark it read-only (a base image is never written), disconnect the
#                    nbd clients. After this the base is a first-class local image,
#                    indistinguishable from a synced one.
#
# The controller enables hydration + polls percent BETWEEN prepare and finalize via
# migration-poll-hydration.py (same as the VM disk), keyed on the base clone name.
#
# Inputs:
#   image_name    - base image name; the LV becomes atlas-image-<image_name>
#   disk_gb       - the base LV size in GiB (source's blockdev size, controller passes)
#   source_host   - source server public IPv4 (plain-TCP NBD)
#   nbd_port      - source base NBD port (image-dir tar served on nbd_port+1)
#   phase         - "prepare" | "finalize"

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import image_directory

REGION_SECTORS = 32768  # 16 MiB dm-clone region, matching clone-target.

# Base-image clone artifacts, keyed by image name (NOT a VM uuid) so a base ship is
# fully decoupled from any per-VM disk clone running concurrently.
BASE_KEY = "base-{image}"
CLONE_DEV = "atlas-{key}-clone"
CLONE_META = "atlas-clonemeta-{key}"

# nbd-client slots for the base ship, WITHIN this VM's contiguous per-VM block:
# root = base+0, data = base+1 (the disk clone's), base image = base+2, image-dir
# tar = base+3. Keying off the same per-VM base slot the disk clone uses is what
# lets several migrations to one target run without sharing an nbd device.
BASE_SLOT_OFFSET = 2
META_SLOT_OFFSET = 3


@dataclass(frozen=True)
class ReceiveBaseInputs(TaskInputs):
	"""Hydrate a local base image from the source over NBD onto this target."""

	command: typing.ClassVar[str] = "migration-receive-base"
	image_name: str
	disk_gb: int
	source_host: str
	nbd_port: int
	nbd_base_slot: int = 0
	phase: str = "prepare"

	@property
	def base_nbd_slot(self) -> int:
		return self.nbd_base_slot + BASE_SLOT_OFFSET

	@property
	def meta_nbd_slot(self) -> int:
		return self.nbd_base_slot + META_SLOT_OFFSET


def main() -> None:
	inputs = ReceiveBaseInputs.from_args()
	pool = ThinPool()
	key = BASE_KEY.format(image=inputs.image_name)

	# Already fully received? A present, read-only base LV means a prior finalize
	# completed — nothing to do on either phase (idempotent no-op).
	base = pool.base_image(inputs.image_name)
	if base.exists and _is_read_only(pool, base):
		print(f"Base {base.name} already present and read-only; nothing to ship.")
		return

	if inputs.phase == "prepare":
		_prepare(inputs, pool, key)
	elif inputs.phase == "finalize":
		_finalize(inputs, pool, key)
	else:
		sys.exit(f"unknown phase {inputs.phase!r} (expected 'prepare' or 'finalize')")


def _prepare(inputs: "ReceiveBaseInputs", pool: "ThinPool", key: str) -> None:
	# Migration-dep pre-flight, same as clone-target.
	for module in ("nbd", "dm_clone"):
		if not run_ok("sudo modprobe {}", module):
			sys.exit(f"kernel module {module!r} unavailable; re-bootstrap before migrating (spec/24)")
	if not run_ok("which nbd-client"):
		sys.exit("nbd-client not installed on the target; re-bootstrap (spec/24)")

	if pool.usage.data_percent >= 80.0:
		sys.exit("target thin pool above 80%; free space before hydrating a base image onto it")

	# 1. A writable thin LV the base hydrates INTO (collapsed + flipped read-only at
	#    finalize). Named atlas-image-<name> so finalize's collapse lands the base at
	#    exactly the path provision-vm / clone-target expect.
	dest = pool.base_image(inputs.image_name)
	pool.create_thin(dest, inputs.disk_gb)

	# 2. nbd client to the source base export, then the dm-clone read-through. Verify
	#    the connected size matches the dest base LV so a stale device can't slip in.
	base_nbd = _ensure_nbd_client(
		inputs.source_host, inputs.nbd_port, inputs.base_nbd_slot, expected_bytes=dest.size_bytes
	)
	_ensure_dm_clone(pool, key, dest, base_nbd)

	# 3. The image directory (kernel + rootfs sentinel), extracted from the meta NBD
	#    export. Small and instant — done here, not gated on hydration.
	_receive_image_dir(inputs)

	print(
		f"Prepared base dm-clone for {inputs.image_name} reading through {inputs.source_host}:{inputs.nbd_port}."
	)


def _finalize(inputs: "ReceiveBaseInputs", pool: "ThinPool", key: str) -> None:
	name = CLONE_DEV.format(key=key)
	if run_ok("sudo dmsetup info {}", name):
		# Guard 100% before collapsing — a partial collapse would leave holes reading
		# through a torn-down NBD (same rule as the VM-disk cutover collapse).
		status = run("sudo dmsetup status {}", name).strip()
		if not _fully_hydrated(status):
			sys.exit(f"base {inputs.image_name} not fully hydrated yet; refusing to collapse ({status})")
		run("sudo dmsetup remove {}", name)
		meta = pool.from_device(f"/dev/atlas/{CLONE_META.format(key=key)}")
		if meta.exists:
			meta.remove()

	# The base is never written after this — flip it read-only, exactly like a
	# file-synced or snapshot-promoted base image.
	base = pool.base_image(inputs.image_name)
	if base.exists and not _is_read_only(pool, base):
		run("sudo lvchange --permission r {}", f"{pool.volume_group}/{base.name}")

	# Disconnect the nbd clients (best-effort; -d is idempotent on an already-free slot).
	run("sudo nbd-client -d {}", f"/dev/nbd{inputs.base_nbd_slot}", check=False)
	run("sudo nbd-client -d {}", f"/dev/nbd{inputs.meta_nbd_slot}", check=False)

	print(f"Collapsed base {inputs.image_name} to a read-only local base image.")


def _receive_image_dir(inputs: "ReceiveBaseInputs") -> None:
	"""Extract the source's image-directory tar (served on nbd_port+1) into this
	host's image directory. Idempotent: skip if the kernel is already present."""
	image_dir = image_directory(inputs.image_name)
	# Cheap presence check — a populated dir means a prior tick already extracted it.
	if os.path.isdir(image_dir) and os.listdir(image_dir):
		return
	meta_nbd = _ensure_nbd_client(inputs.source_host, inputs.nbd_port + 1, inputs.meta_nbd_slot)
	run("sudo install -d -m 0700 {}", image_dir)
	# The tar was written with `-C <image_dir> .`, so paths are relative — extract
	# straight in. Read the block device (a file-backed NBD export of the tar).
	run("sudo bash -c {}", f"tar -xf {meta_nbd} -C {image_dir}")


# ─────────────────────────────────────────────────────────────────────────────
# Shared with clone-target's shape (kept local so the base ship is self-contained).
# ─────────────────────────────────────────────────────────────────────────────


def _ensure_nbd_client(host: str, port: int, slot: int, expected_bytes: int = 0) -> str:
	"""Size-verified idempotent connect — mirrors clone-target's: reuse an existing
	connection only if its size matches the expected export, else reconnect. Guards
	against a stale/wrong device on this slot (the 'Invalid argument' dm-clone bug)."""
	device = f"/dev/nbd{slot}"
	if run_ok("sudo nbd-client -check {}", device):
		if expected_bytes and _nbd_size_bytes(device) == expected_bytes:
			return device
		run("sudo nbd-client -d {}", device, check=False)
	run("sudo nbd-client -N {} {} {} {} -persist", "", host, str(port), device)
	return device


def _nbd_size_bytes(device: str) -> int:
	out = run("sudo blockdev --getsize64 {}", device, check=False).strip()
	return int(out) if out.isdigit() else 0


def _ensure_dm_clone(pool: "ThinPool", key: str, dest, source_device: str) -> None:
	name = CLONE_DEV.format(key=key)
	if run_ok("sudo dmsetup info {}", name):
		return
	meta = pool.from_device(f"/dev/atlas/{CLONE_META.format(key=key)}")
	if not meta.exists:
		pool.create_thin(meta, 1)
		run("sudo dd if=/dev/zero of={} bs=1M count=16 conv=fsync", meta.device_path)
	sectors = dest.size_bytes // 512
	table = f"0 {sectors} clone {meta.device_path} {dest.device_path} {source_device} {REGION_SECTORS}"
	run("sudo dmsetup create {} --table {}", name, table)


def _fully_hydrated(status_line: str) -> bool:
	"""dm-clone status: the 2nd 'a/b' field is <#hydrated>/<#total_regions>. Reuses
	the same field convention as migration-poll-hydration's parse."""
	pairs = [f for f in status_line.split() if "/" in f and f.replace("/", "").isdigit()]
	if len(pairs) < 2:
		raise ValueError(f"cannot parse dm-clone hydration from: {status_line!r}")
	hydrated, total = (int(x) for x in pairs[1].split("/"))
	return total > 0 and hydrated >= total


def _is_read_only(pool: "ThinPool", lv) -> bool:
	"""True if the LV carries the LVM read-only permission flag ('r' in lv_attr[1])."""
	attr = run("sudo lvs --noheadings -o lv_attr {}", f"{pool.volume_group}/{lv.name}").strip()
	return len(attr) > 1 and attr[1] == "r"


if __name__ == "__main__":
	main()
