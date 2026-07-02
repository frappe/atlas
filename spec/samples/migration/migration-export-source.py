#!/usr/bin/env python3
# Source side of a VM migration: thin-snapshot the Stopped VM's disk(s) and export
# them read-only over NBD, bound to localhost. The target reaches this export
# through an SSH LocalForward tunnel it opens (so there is no public NBD port and
# no firewall hole; spec/06 gives hosts no private fabric, spec/24).
#
# SAMPLE / ILLUSTRATIVE — mirrors the typed-Task contract and the lvm.ThinPool API
# of scripts/provision-vm.py and scripts/snapshot-vm.py. Idempotent: re-running
# re-uses an existing snapshot and an already-serving NBD process.
#
# Inputs:
#   virtual_machine_name  - UUID
#   nbd_port              - localhost port to bind (controller derives it per-UUID)
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

# The thin snapshots a migration takes are NAMED with a -migrate suffix so they
# are unmistakably transient (not a Virtual Machine Snapshot row's atlas-snap-<id>)
# and the cleanup phase can find and lvremove them by a derived name.
ROOT_SNAP = "atlas-snap-{uuid}-migrate"
DATA_SNAP = "atlas-datasnap-{uuid}-migrate"


@dataclass(frozen=True)
class ExportInputs(TaskInputs):
	"""Snapshot the Stopped VM's disk(s) and serve them read-only over NBD on
	localhost for a migration's target host to clone from."""

	command: typing.ClassVar[str] = "migration-export-source"
	virtual_machine_name: str
	nbd_port: int


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
	if pool.usage().too_full_to_snapshot:
		sys.exit("thin pool too full to snapshot for migration; free space first")

	# 1. Root snapshot. snapshot_into is idempotent (re-activates if it exists), so a
	#    re-entry after a crash reuses the same crash-consistent image.
	root_origin = pool.vm_disk(uuid)
	if not root_origin.exists:
		sys.exit(f"VM disk LV not found: {root_origin.name}; is the UUID right and the VM on this host?")
	root_snap = pool.from_device(f"/dev/atlas/{ROOT_SNAP.format(uuid=uuid)}")
	root_origin.snapshot_into(root_snap)
	export_devices = [root_snap.device_path]

	# 2. Data snapshot, if the VM has a data disk. Same idempotent pattern.
	data_snap = None
	data_origin = pool.data_disk(uuid)
	if data_origin.exists:
		data_snap = pool.from_device(f"/dev/atlas/{DATA_SNAP.format(uuid=uuid)}")
		data_origin.snapshot_into(data_snap)
		export_devices.append(data_snap.device_path)

	# 3. NBD export, read-only, bound to localhost. Idempotent: if a server is
	#    already serving this VM's export on this port, reuse it (don't double-bind).
	#    qemu-nbd is one process per device; we run one per disk on adjacent ports
	#    (root = nbd_port, data = nbd_port+1) so the target opens two nbd clients.
	nbd_pid = _ensure_nbd_export(root_snap.device_path, inputs.nbd_port)
	if data_snap is not None:
		_ensure_nbd_export(data_snap.device_path, inputs.nbd_port + 1)

	ExportResult(
		nbd_port=inputs.nbd_port,
		nbd_pid=nbd_pid,
		root_size_bytes=root_snap.size_bytes,
		data_size_bytes=data_snap.size_bytes if data_snap else 0,
	).emit()
	print(f"Exported {uuid} root (+data) over NBD on 127.0.0.1:{inputs.nbd_port}.")


def _ensure_nbd_export(device: str, port: int) -> int:
	"""Serve `device` read-only over NBD on 127.0.0.1:`port`. Returns the server pid.
	Idempotent: if a qemu-nbd is already bound to this port for this device, return
	its pid instead of starting a second one (which would EADDRINUSE)."""
	existing = run("sudo", "bash", "-lc", f"ss -ltnp 'sport = :{port}' || true").strip()
	if f":{port}" in existing:
		# Already bound — recover the pid for the result/cleanup. (qemu-nbd writes a
		# pidfile; the sample keys off a derived pidfile path.)
		pidfile = _pidfile(port)
		if run_ok("sudo", "test", "-f", pidfile):
			return int(run("sudo", "cat", pidfile).strip())
		return 0
	# --persistent so a transient client disconnect (the SSH tunnel re-dialing)
	# doesn't tear the export down; --read-only because the source is the source of
	# truth and must never be written by the target; --cache=none for correctness.
	pidfile = _pidfile(port)
	run(
		"sudo",
		"qemu-nbd",
		"--persistent",
		"--read-only",
		"--cache=none",
		"--bind=127.0.0.1",
		f"--port={port}",
		f"--pid-file={pidfile}",
		"--fork",
		device,
	)
	return int(run("sudo", "cat", pidfile).strip())


def _pidfile(port: int) -> str:
	return f"/var/lib/atlas/run/migrate-nbd-{port}.pid"


if __name__ == "__main__":
	main()
