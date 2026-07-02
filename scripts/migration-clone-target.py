#!/usr/bin/env python3
# Target side of a VM migration (spec/19), PREPARE phase: pre-flight the migration
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

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import image_directory

REGION_SECTORS = 32768  # 16 MiB dm-clone region (spec/19); tunable.
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
				f"and re-bootstrap before migrating (spec/19)"
			)
	if not run_ok("which nbd-client"):
		sys.exit("nbd-client not installed on the target; re-bootstrap (spec/19)")

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

	# 4. nbd clients straight to the source over plain TCP (no tunnel this stage).
	root_nbd = _ensure_nbd_client(inputs.source_host, inputs.nbd_port, slot=0)
	data_nbd = None
	if data_dest is not None:
		data_nbd = _ensure_nbd_client(inputs.source_host, inputs.nbd_port + 1, slot=1)

	# 5. dm-clone device(s). Idempotent: skip if the mapper device already exists.
	_ensure_dm_clone(pool, uuid, dest, root_nbd)
	if data_dest is not None:
		_ensure_dm_clone(pool, uuid + "-data", data_dest, data_nbd)

	print(f"Prepared dm-clone for {uuid} reading through {inputs.source_host}:{inputs.nbd_port}.")


def _ensure_nbd_client(host: str, port: int, slot: int) -> str:
	"""Attach /dev/nbd<slot> to the source export. Idempotent: if already connected,
	return it. Returns the /dev/nbdN path used as the dm-clone source."""
	device = f"/dev/nbd{slot}"
	if run_ok("sudo nbd-client -check {}", device):
		return device
	# -N "" default export; -persist so a transient blip re-dials rather than dropping.
	run("sudo nbd-client -N {} {} {} {} -persist", "", host, str(port), device)
	return device


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
