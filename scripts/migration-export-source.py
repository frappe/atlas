#!/usr/bin/env python3
# Source side of a VM migration (spec/19): thin-snapshot the Stopped VM's disk(s)
# and export them read-only over NBD.
#
# STAGE 1 transport: plain TCP. qemu-nbd binds the source's PUBLIC IPv4 and the
# target's nbd-client dials it directly — no SSH tunnel yet (the host-to-host
# credential is a deferred stage-3 prerequisite, spec/19 §2.1). This data path is
# UNENCRYPTED; it is a deliberate get-it-working-first shortcut.
#
# Idempotent: re-running re-uses an existing snapshot and an already-serving NBD
# process (keyed by the pidfile + a listening-port check).
#
# Inputs:
#   virtual_machine_name  - UUID
#   nbd_port              - TCP port to bind (controller derives it per-UUID)
#   bind_address          - address qemu-nbd binds (the source's public IPv4)
#
# Emits ATLAS_RESULT={"nbd_port": N, "nbd_pid": P, "root_size_bytes": B,
#                     "data_size_bytes": B_or_0}

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool

# Named with a -migrate suffix so they are unmistakably transient (not a Virtual
# Machine Snapshot row's atlas-snap-<id>) and cleanup can lvremove them by name.
ROOT_SNAP = "atlas-snap-{uuid}-migrate"
DATA_SNAP = "atlas-datasnap-{uuid}-migrate"

RUN_DIRECTORY = "/var/lib/atlas/run"


@dataclass(frozen=True)
class ExportInputs(TaskInputs):
	"""Snapshot the Stopped VM's disk(s) and serve them read-only over NBD for a
	migration's target host to clone from."""

	command: typing.ClassVar[str] = "migration-export-source"
	virtual_machine_name: str
	nbd_port: int
	bind_address: str


@dataclass(frozen=True)
class ExportResult(TaskResult):
	nbd_port: int
	nbd_pid: int
	root_size_bytes: int
	data_size_bytes: int = 0


def main() -> None:
	inputs = ExportInputs.from_args()
	pool = ThinPool()
	uuid = inputs.virtual_machine_name

	# Pool-fullness guard, same as snapshot-vm.py: a thin snapshot is free up front
	# but every later CoW write allocates; don't snapshot an almost-full pool.
	if pool.usage.too_full_to_snapshot:
		sys.exit("thin pool too full to snapshot for migration; free space first")

	run("sudo mkdir -p {}", RUN_DIRECTORY)

	# 1. Root snapshot. snapshot_into is idempotent (re-activates if it exists), so a
	#    re-entry after a crash reuses the same crash-consistent image.
	root_origin = pool.vm_disk(uuid)
	if not root_origin.exists:
		sys.exit(f"VM disk LV not found: {root_origin.name}; is the UUID right and the VM on this host?")
	root_snap = pool.from_device(f"/dev/atlas/{ROOT_SNAP.format(uuid=uuid)}")
	root_origin.snapshot_into(root_snap)

	# 2. Data snapshot, if the VM has a data disk. Same idempotent pattern.
	data_snap = None
	data_origin = pool.data_disk(uuid)
	if data_origin.exists:
		data_snap = pool.from_device(f"/dev/atlas/{DATA_SNAP.format(uuid=uuid)}")
		data_origin.snapshot_into(data_snap)

	# 3. NBD export, read-only, bound to the source's public IPv4 (plain TCP). One
	#    qemu-nbd per disk on adjacent ports (root = nbd_port, data = nbd_port+1).
	nbd_pid = _ensure_nbd_export(root_snap.device_path, inputs.bind_address, inputs.nbd_port)
	if data_snap is not None:
		_ensure_nbd_export(data_snap.device_path, inputs.bind_address, inputs.nbd_port + 1)

	ExportResult(
		nbd_port=inputs.nbd_port,
		nbd_pid=nbd_pid,
		root_size_bytes=root_snap.size_bytes,
		data_size_bytes=data_snap.size_bytes if data_snap else 0,
	).emit()
	print(f"Exported {uuid} root (+data) over NBD on {inputs.bind_address}:{inputs.nbd_port}.")


def _ensure_nbd_export(device: str, bind_address: str, port: int) -> int:
	"""Serve `device` read-only over NBD on bind_address:port. Returns the server pid.
	Idempotent: if a qemu-nbd is already bound to this port, return its pid instead
	of starting a second one (which would EADDRINUSE)."""
	pidfile = _pidfile(port)
	listening = run("sudo bash -c {}", f"ss -ltn 'sport = :{port}' || true").strip()
	if f":{port}" in listening:
		if run_ok("sudo test -f {}", pidfile):
			return int(run("sudo cat {}", pidfile).strip())
		return 0
	# --persistent so a transient client disconnect doesn't tear the export down;
	# --read-only because the source is the source of truth; --fork returns once the
	# socket is ready. systemd-run detaches it fully from this SSH session (a bare
	# `&` dies on session close — verified on the real hosts). qemu-nbd's own --fork
	# double-forks, but wrapping in a transient scope makes the pidfile reliable.
	run(
		"sudo qemu-nbd --persistent --read-only --cache=none --bind={} --port={} --pid-file={} --fork {}",
		bind_address,
		str(port),
		pidfile,
		device,
	)
	return int(run("sudo cat {}", pidfile).strip())


def _pidfile(port: int) -> str:
	return f"{RUN_DIRECTORY}/migrate-nbd-{port}.pid"


if __name__ == "__main__":
	main()
