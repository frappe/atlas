#!/usr/bin/env python3
# Target side of a VM migration. Two phases share this one script (PHASE flag):
#   prepare - pre-flight (kernel modules / image / pool), create fresh thin LVs,
#             open the SSH tunnel to the source NBD, build the dm-clone device(s).
#   inject  - mount the (clone-backed) disk and rewrite the guest network env with
#             the NEW /128 + NAT44 /30, host keys PRESERVED — exactly the rebuild
#             path. Done before any boot so the guest never comes up on stale env.
#
# SAMPLE / ILLUSTRATIVE — reuses scripts/lib/atlas/{lvm,rootfs,paths}.py exactly as
# provision-vm.py / rebuild-vm.py do. Every step is idempotent (checks its
# artifact before acting), so a re-entry after a crash is a cheap no-op up to where
# it got.
#
# dm-clone primer: `dmsetup create <name> --table "0 <sectors> clone <meta>
# <dest> <source> <region_sectors>"` makes a device that serves reads from <source>
# (the NBD-backed source snapshot) until a region is hydrated, lands all writes on
# <dest> (the local thin LV), and — once `enable_hydration` is messaged — copies
# every region from source to dest in the background. At 100% the device is pure
# local and can be collapsed (`dmsetup remove`), leaving the plain thin LV.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import VirtualMachinePaths, image_directory
from atlas.rootfs import Identity, inject_identity

REGION_SECTORS = 32768  # 16 MiB dm-clone region (see spec/24); tunable.
CLONE_META = "atlas-clonemeta-{uuid}"
CLONE_DEV = "atlas-vm-{uuid}-clone"


@dataclass(frozen=True)
class CloneInputs(TaskInputs):
	"""Build (and identity-inject) the target side of a migrated VM's disk."""

	command: typing.ClassVar[str] = "migration-clone-target"
	virtual_machine_name: str
	image_name: str
	disk_gb: int
	phase: str  # "prepare" | "inject"
	# prepare-only:
	source_host: str = ""  # source server's public v4, for the SSH tunnel
	nbd_port: int = 0
	data_disk_gb: int = 0
	# inject-only (the new identity; see provision-vm.py's ProvisionInputs):
	virtual_machine_ipv6: str = ""
	ipv4_guest_cidr: str = ""
	ipv4_gateway: str = ""
	ssh_public_key: str = ""
	atlas_fc_uid: int = 0
	data_disk_mount_at: str = ""
	routing_base_url: str = ""


def main() -> None:
	inputs = CloneInputs.from_args()
	if inputs.phase == "prepare":
		_prepare(inputs)
	elif inputs.phase == "inject":
		_inject(inputs)
	else:
		sys.exit(f"unknown phase {inputs.phase!r} (expected 'prepare' or 'inject')")


def _prepare(inputs: "CloneInputs") -> None:
	pool = ThinPool()
	uuid = inputs.virtual_machine_name

	# 0. New-dependency pre-flight. nbd + dm_clone are NOT in the base bootstrap dep
	#    set (spec README principle 5); fail loud here, not late in dmsetup.
	for module in ("nbd", "dm_clone"):
		if not run_ok("sudo", "modprobe", module):
			sys.exit(
				f"kernel module {module!r} unavailable on this host; install linux-modules-extra "
				f"and re-bootstrap before migrating (spec/24)"
			)
	if not run_ok("which", "qemu-nbd"):
		sys.exit("qemu-nbd not installed on the target; install it and re-bootstrap (spec/24)")

	# 1. Image present (kernel + CoW origin come from it), same probe as
	#    provision-vm.py step 0. Fail loud → operator runs Sync to Server first.
	image = image_directory(inputs.image_name)
	if not pool.base_image(inputs.image_name).exists:
		sys.exit(f"base image LV not on target: atlas-image-{inputs.image_name}; run Sync to Server first")
	if not os.path.isdir(image):
		sys.exit(f"image directory {image} missing on target; run Sync to Server first")

	# 2. Pool headroom for hydration's CoW writes.
	if pool.usage().data_percent >= 80.0:
		sys.exit("target thin pool above 80%; free space before hydrating a migration onto it")

	# 3. Fresh local thin LVs the clone hydrates INTO. create_thin is idempotent.
	dest = pool.vm_disk(uuid)
	pool.create_thin(dest, inputs.disk_gb)
	data_dest = None
	if inputs.data_disk_gb > 0:
		data_dest = pool.data_disk(uuid)
		pool.create_thin(data_dest, inputs.data_disk_gb)

	# 4. SSH tunnel to the source NBD + nbd clients. Idempotent helpers.
	_ensure_tunnel(inputs.source_host, inputs.nbd_port, inputs.nbd_port)  # root
	root_nbd = _ensure_nbd_client(inputs.nbd_port, slot=0)
	if data_dest is not None:
		_ensure_tunnel(inputs.source_host, inputs.nbd_port + 1, inputs.nbd_port + 1)
		data_nbd = _ensure_nbd_client(inputs.nbd_port + 1, slot=1)

	# 5. dm-clone device(s). Idempotent: skip if the mapper device already exists.
	_ensure_dm_clone(pool, uuid, dest, root_nbd)
	if data_dest is not None:
		_ensure_dm_clone(pool, uuid + "-data", data_dest, data_nbd)

	print(f"Prepared dm-clone for {uuid} reading through 127.0.0.1:{inputs.nbd_port}.")


def _inject(inputs: "CloneInputs") -> None:
	# The disk to write identity into is the dm-clone device (writes land on the
	# local dest LV; reads through to source). Mount it, rewrite the guest network
	# env with the NEW address, host keys PRESERVED — identical to rebuild's
	# inject_identity(..., regenerate_host_keys=False). Idempotent: inject_identity
	# overwrites the env file, so a re-run lands the same bytes.
	uuid = inputs.virtual_machine_name
	clone_device = f"/dev/mapper/{CLONE_DEV.format(uuid=uuid)}"
	if not os.path.exists(clone_device):
		sys.exit(f"dm-clone device {clone_device} absent; run phase=prepare first")
	inject_identity(
		clone_device,
		Identity(
			uuid=uuid,
			ipv6_address=inputs.virtual_machine_ipv6,
			ssh_public_key=inputs.ssh_public_key,
			ipv4_guest_cidr=inputs.ipv4_guest_cidr,
			ipv4_gateway=inputs.ipv4_gateway,
			data_disk_mount_at=inputs.data_disk_mount_at,
			routing_base_url=inputs.routing_base_url,
		),
		# CRITICAL: keep the migrated disk's existing SSH host keys — the VM's SSH
		# identity must not change across a move (clients' known_hosts), exactly as
		# rebuild/restore preserve them. Only provision/clone regenerate.
		regenerate_host_keys=False,
	)
	# Write the per-VM network.env sidecar (vm-network-up.py reads it on the unit's
	# next start to build the new /128's netns/route/proxy-NDP). In a full impl this
	# reuses provision-vm.py's _network_env writer; elided in the sample.
	print(f"Injected new identity ({inputs.virtual_machine_ipv6}) into {uuid}, host keys preserved.")


def _ensure_tunnel(source_host: str, local_port: int, remote_port: int) -> None:
	"""Open (idempotently) an SSH LocalForward 127.0.0.1:local_port ->
	source:127.0.0.1:remote_port using the durable Atlas key, so the source's
	localhost-bound NBD export is reachable here with no public port. A real impl
	uses a controlmaster + a recorded control socket; the sample shows the shape."""
	if run_ok("sudo", "bash", "-lc", f"ss -ltn 'sport = :{local_port}' | grep -q :{local_port}"):
		return  # tunnel already up
	run(
		"sudo",
		"bash",
		"-lc",
		# -f background, -N no command, ExitOnForwardFailure so a bad forward fails
		# loudly. Key + known_hosts handling matches the Atlas SSH transport.
		f"ssh -f -N -o ExitOnForwardFailure=yes "
		f"-L 127.0.0.1:{local_port}:127.0.0.1:{remote_port} root@{source_host}",
	)


def _ensure_nbd_client(port: int, slot: int) -> str:
	"""Attach /dev/nbd<slot> to the tunneled export. Idempotent: if already
	connected, return it. Returns the /dev/nbdN path used as the dm-clone source."""
	device = f"/dev/nbd{slot}"
	if run_ok("sudo", "bash", "-lc", f"nbd-client -check {device}"):
		return device
	run("sudo", "nbd-client", "-N", "", "127.0.0.1", str(port), device, "-persist")
	return device


def _ensure_dm_clone(pool: "ThinPool", key: str, dest, source_device: str) -> None:
	"""Create the dm-clone mapping if absent. dest is a LogicalVolume (the local thin
	LV); source_device is the /dev/nbdN reading through to the source snapshot."""
	name = CLONE_DEV.format(uuid=key)
	if run_ok("sudo", "dmsetup", "info", name):
		return  # already created (idempotent)
	# A small zeroed metadata device. dm-clone needs ~ (dev_size / region_size) bits;
	# a 16 MiB LV is plenty for any VM disk we host. Zero it once (dm-clone refuses
	# stale metadata).
	meta = pool.from_device(f"/dev/atlas/{CLONE_META.format(uuid=key)}")
	if not meta.exists:
		pool.create_thin(meta, 1)  # 1 GiB thin; only the first 16 MiB is used
		run("sudo", "dd", "if=/dev/zero", f"of={meta.device_path}", "bs=1M", "count=16", "conv=fsync")
	sectors = dest.size_bytes // 512
	table = f"0 {sectors} clone {meta.device_path} {dest.device_path} {source_device} {REGION_SECTORS}"
	run("sudo", "dmsetup", "create", name, "--table", table)


if __name__ == "__main__":
	main()
