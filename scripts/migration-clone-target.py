#!/usr/bin/env python3
# Target side of a VM migration (spec/24), PREPARE phase: pre-flight the migration
# deps, create fresh local thin LV(s), connect an nbd client to the source's NBD
# export over plain TCP (stage 1 — no SSH tunnel yet, §2.1), and build the dm-clone
# device(s). The target VM's disk then reads-through to the source over NBD while
# `migration-poll-hydration` copies every block locally in the background.
#
# The identity inject + unit launch are NOT here: they run at cutover, once the
# dm-clone is collapsed, by re-using provision-vm (with preserve_host_keys=1). This
# script only lays the read-through disk down.
#
# SAMPLE dm-clone primer: `dmsetup create <name> --table "0 <sectors> clone <meta>
# <dest> <source> <region_sectors>"` serves reads from <source> (the NBD-backed
# source snapshot) until a region is hydrated, lands all writes on <dest> (the local
# thin LV), and — once `enable_hydration` is messaged — copies every region in the
# background. At 100% it can be collapsed (dmsetup remove), leaving the plain LV.
#
# Idempotent: every step checks its artifact before acting.
#
# Inputs:
#   virtual_machine_name  - UUID
#   image_name            - base image (kernel presence pre-flight)
#   disk_gb               - root disk size (>= source)
#   data_disk_gb          - data disk size, 0 if none
#   source_host           - source server public IPv4 (plain-TCP NBD target)
#   nbd_port              - source NBD port (data disk served on nbd_port+1)
#   nbd_base_slot         - first of this VM's contiguous nbd CLIENT devices on the
#                           target (root = base+0, data = base+1). Per-VM so two
#                           migrations to one target never share an nbd device.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import image_directory

REGION_SECTORS = 32768  # 16 MiB dm-clone region (spec/24); tunable.
CLONE_META = "atlas-clonemeta-{key}"
CLONE_DEV = "atlas-vm-{key}-clone"


@dataclass(frozen=True)
class CloneInputs(TaskInputs):
	"""Build the read-through dm-clone target for a migrated VM's disk(s)."""

	command: typing.ClassVar[str] = "migration-clone-target"
	virtual_machine_name: str
	image_name: str
	disk_gb: int
	source_host: str
	nbd_port: int
	data_disk_gb: int = 0
	nbd_base_slot: int = 0
	phase: str = "prepare"  # only "prepare" in stage 1


def main() -> None:
	inputs = CloneInputs.from_args()
	if inputs.phase != "prepare":
		sys.exit(f"unknown phase {inputs.phase!r} (stage 1 supports only 'prepare')")

	pool = ThinPool()
	uuid = inputs.virtual_machine_name

	# 0. Migration-dep pre-flight. These ship at bootstrap now, but re-assert loud
	#    here rather than fail deep in dmsetup/nbd-client.
	for module in ("nbd", "dm_clone"):
		if not run_ok("sudo modprobe {}", module):
			sys.exit(
				f"kernel module {module!r} unavailable; install linux-modules-extra "
				f"and re-bootstrap before migrating (spec/24)"
			)
	if not run_ok("which nbd-client"):
		sys.exit("nbd-client not installed on the target; re-bootstrap (spec/24)")

	# 1. Image present (the kernel comes from it at cutover), same probe as
	#    provision-vm.py step 0.
	image = image_directory(inputs.image_name)
	if not pool.base_image(inputs.image_name).exists:
		sys.exit(f"base image LV not on target: atlas-image-{inputs.image_name}; run Sync to Server first")
	if not os.path.isdir(image):
		sys.exit(f"image directory {image} missing on target; run Sync to Server first")

	# 2. Pool headroom for hydration's CoW writes.
	if pool.usage.data_percent >= 80.0:
		sys.exit("target thin pool above 80%; free space before hydrating a migration onto it")

	# 3. Fresh local thin LV(s) the clone hydrates INTO. create_thin is idempotent.
	dest = pool.vm_disk(uuid)
	pool.create_thin(dest, inputs.disk_gb)
	data_dest = None
	if inputs.data_disk_gb > 0:
		data_dest = pool.data_disk(uuid)
		pool.create_thin(data_dest, inputs.data_disk_gb)

	# 4. Repair a wedged stack before (re)building it. A dm-clone whose source nbd
	#    client has DIED reads 0 bytes and freezes hydration; the fix is to rebuild
	#    the stack, but the clone pins the nbd device open, so the clone must come
	#    down FIRST — only then can the dead client be disconnected and re-dialed.
	_drop_clone_if_source_dead(uuid, inputs.nbd_base_slot)
	if data_dest is not None:
		_drop_clone_if_source_dead(uuid + "-data", inputs.nbd_base_slot + 1)

	# 5. nbd clients straight to the source over plain TCP (no tunnel this stage).
	root_nbd = _ensure_nbd_client(
		inputs.source_host, inputs.nbd_port, slot=inputs.nbd_base_slot, expected_bytes=dest.size_bytes
	)
	data_nbd = None
	if data_dest is not None:
		data_nbd = _ensure_nbd_client(
			inputs.source_host,
			inputs.nbd_port + 1,
			slot=inputs.nbd_base_slot + 1,
			expected_bytes=data_dest.size_bytes,
		)

	# 6. dm-clone device(s). Idempotent: skip if the mapper device already exists
	#    (a healthy one was left alone in step 4; a wedged one was removed and is
	#    rebuilt here onto the freshly-reconnected client).
	_ensure_dm_clone(pool, uuid, dest, root_nbd)
	if data_dest is not None:
		_ensure_dm_clone(pool, uuid + "-data", data_dest, data_nbd)

	print(f"Prepared dm-clone for {uuid} reading through {inputs.source_host}:{inputs.nbd_port}.")


def _ensure_nbd_client(host: str, port: int, slot: int, expected_bytes: int = 0) -> str:
	"""Attach /dev/nbd<slot> to the source export. Returns the /dev/nbdN path used as
	the dm-clone source.

	Idempotent, health-checked AND size-verified: a slot is trusted only when its
	client is ALIVE and its size matches `expected_bytes` (the source disk's size).

	- LIVENESS: `nbd-client -check` lies — it reports "connected" off the kernel's
	  stale binding even after the client PROCESS has died (seen on a real f1→f2
	  migration 2026-07-02: dead pid, -check exit 0, every read 0 bytes, dm-clone
	  frozen). So we read the true owner from /sys/block/nbdN/pid and confirm that
	  process still exists; a dead owner is a wedged device we must re-dial.
	- SIZE: a stale/wrong connection (a prior migration's leftover) has the wrong
	  size, and a dm-clone whose source is smaller than the dest fails deep in
	  dmsetup. `expected_bytes=0` skips this check (caller doesn't know the size).

	On either failure we disconnect and reconnect to the intended export. Note the
	disconnect only succeeds if nothing is stacked on the device — a caller repairing
	a wedged client under a live dm-clone must remove that clone FIRST (see
	migration-poll-hydration's recover step)."""
	device = f"/dev/nbd{slot}"
	if _nbd_client_alive(slot):
		if expected_bytes and _nbd_size_bytes(device) == expected_bytes:
			return device
		# Wrong export (or unknown size): drop it and reconnect to the intended one.
		run("sudo nbd-client -d {}", device, check=False)
	else:
		# Dead/stale binding: -d clears the kernel's zombie owner so the reconnect
		# below can take the slot. Harmless if already clear.
		run("sudo nbd-client -d {}", device, check=False)
	# -N "" default export; -persist so a transient blip re-dials rather than dropping.
	run("sudo nbd-client -N {} {} {} {} -persist", "", host, str(port), device)
	return device


def _drop_clone_if_source_dead(key: str, slot: int) -> None:
	"""Tear down a dm-clone ONLY when its source nbd client has died — the wedged
	state that freezes hydration (source reads return 0 bytes). Removing it frees the
	nbd device so `_ensure_nbd_client` can re-dial and `_ensure_dm_clone` can rebuild.

	A HEALTHY clone (live client) is left untouched — this is idempotent and must not
	discard good hydration progress. A dead client under a live clone can't be fixed
	any other way: the clone pins the nbd device open, so the client can't be
	disconnected until the clone comes down (proven on a real f1→f2 migration
	2026-07-02). The rebuilt clone re-hydrates from 0 — correctness over speed."""
	name = CLONE_DEV.format(key=key)
	if not run_ok("sudo dmsetup info {}", name):
		return  # no clone yet — nothing wedged
	if _nbd_client_alive(slot):
		return  # source client alive — healthy, leave it
	run("sudo dmsetup remove {}", name, check=False)


def _nbd_client_alive(slot: int) -> bool:
	"""Whether /dev/nbd<slot> has a LIVE client process. Reads the owning pid the
	kernel records in /sys/block/nbd<slot>/pid and checks the process still exists —
	the reliable signal `nbd-client -check` fails to give (it trusts a stale binding
	whose process has died)."""
	pid = run("cat /sys/block/nbd{}/pid", str(slot), check=False).strip()
	if not pid.isdigit():
		return False  # no owner recorded → not connected
	# A live client's pid has a /proc entry; a dead one's does not.
	return run_ok("test -d /proc/{}", pid)


def _nbd_size_bytes(device: str) -> int:
	"""Byte size of a connected nbd device (0 if it can't be read)."""
	out = run("sudo blockdev --getsize64 {}", device, check=False).strip()
	return int(out) if out.isdigit() else 0


def _ensure_dm_clone(pool: "ThinPool", key: str, dest, source_device: str) -> None:
	"""Create the dm-clone mapping if absent. dest is the local thin LV; source_device
	is /dev/nbdN reading through to the source snapshot."""
	name = CLONE_DEV.format(key=key)
	if run_ok("sudo dmsetup info {}", name):
		return  # already created (idempotent)
	# A small zeroed metadata device. dm-clone needs ~(dev_size / region_size) bits;
	# 16 MiB is plenty for any VM disk we host. Zero it once (dm-clone refuses stale
	# metadata).
	meta = pool.from_device(f"/dev/atlas/{CLONE_META.format(key=key)}")
	if not meta.exists:
		pool.create_thin(meta, 1)  # 1 GiB thin; only the first 16 MiB is used
		run("sudo dd if=/dev/zero of={} bs=1M count=16 conv=fsync", meta.device_path)
	sectors = dest.size_bytes // 512
	table = f"0 {sectors} clone {meta.device_path} {dest.device_path} {source_device} {REGION_SECTORS}"
	run("sudo dmsetup create {} --table {}", name, table)


if __name__ == "__main__":
	main()
