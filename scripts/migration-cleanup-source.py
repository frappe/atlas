#!/usr/bin/env python3
# Source side of a VM migration (spec/24), CLEANUP phase: after the target VM is
# confirmed Running and the routes are re-pointed, tear the source copy down. This
# runs LAST and only after cutover, so it is safe to destroy the source state:
#   1. Kill the qemu-nbd export(s): root on nbd_port, data on nbd_port+1, and — on
#      the local-image path (spec/24 §5.1) — the base LV export on nbd_port+2 and
#      the image-dir tar export on nbd_port+3 (plus the staged tar file). The base
#      LV itself is the source's own immutable image and is NEVER removed.
#   2. lvremove the transient -migrate thin snapshots.
#   3. Tear down the stale source VM: disable the unit, run vm-network-down, remove
#      the VM directory (jail tree) and the disk LV(s) — the same teardown
#      terminate-vm.py does, so the source host is left as if the VM had never been
#      there (its identity now lives on the target).
#
# Idempotent + best-effort (check=False on teardown pokes): a re-entry after a
# partial cleanup just finishes the rest.
#
# Inputs:
#   virtual_machine_name  - UUID
#   nbd_port              - source NBD port (data on nbd_port+1)
#   nbd_pid               - recorded qemu-nbd pid (fallback: pidfile / pkill by port)

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok, shell
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import ATLAS_PYTHON, VirtualMachinePaths

ROOT_SNAP = "atlas-snap-{uuid}-migrate"
DATA_SNAP = "atlas-datasnap-{uuid}-migrate"
RUN_DIRECTORY = "/var/lib/atlas/run"


@dataclass(frozen=True)
class CleanupInputs(TaskInputs):
	"""Tear down a migrated VM's source-side copy (NBD, snapshots, unit, disk)."""

	command: typing.ClassVar[str] = "migration-cleanup-source"
	virtual_machine_name: str
	nbd_port: int = 0
	nbd_pid: int = 0


def main() -> None:
	inputs = CleanupInputs.from_args()
	pool = ThinPool()
	uuid = inputs.virtual_machine_name
	paths = VirtualMachinePaths(uuid)

	# 1. Kill the NBD export(s). By recorded pid first; fall back to the pidfile per
	#    port so a lost nbd_pid still cleans up.
	_kill_nbd(inputs.nbd_pid, inputs.nbd_port)
	if inputs.nbd_port:
		_kill_nbd(0, inputs.nbd_port + 1)  # data disk export, if any
		# Local-image base ship exports (harmless no-ops if this migration wasn't one):
		# base LV on +2, image-dir tar on +3. The base LV is immutable and stays put.
		_kill_nbd(0, inputs.nbd_port + 2)
		_kill_nbd(0, inputs.nbd_port + 3)
		# The staged image-dir tars (glob needs a shell; the literal is ours, no params).
		shell(f"sudo rm -f {RUN_DIRECTORY}/migrate-base-meta-*.tar", check=False)

	# 2. Remove the transient migration snapshots (guarded lvremove; no-op if gone).
	pool.from_device(f"/dev/atlas/{ROOT_SNAP.format(uuid=uuid)}").remove()
	pool.from_device(f"/dev/atlas/{DATA_SNAP.format(uuid=uuid)}").remove()

	# 3. Tear down the stale source VM — the terminate-vm.py teardown, verbatim in
	#    shape. Best-effort: the unit may already be gone.
	run("sudo systemctl disable --now {}", paths.systemd_unit, check=False)
	if run_ok("sudo test -f {}", paths.network_env):
		run(
			"sudo {} /var/lib/atlas/bin/vm-network-down.py {}",
			ATLAS_PYTHON,
			uuid,
			check=False,
		)
	run("sudo rm -rf {}", paths.directory)
	pool.vm_disk(uuid).remove()
	pool.data_disk(uuid).remove()

	print(f"Cleaned up source copy of {uuid} (NBD, snapshots, unit, disk).")


def _kill_nbd(pid: int, port: int) -> None:
	if pid:
		run("sudo kill {}", str(pid), check=False)
	pidfile = f"{RUN_DIRECTORY}/migrate-nbd-{port}.pid"
	if run_ok("sudo test -f {}", pidfile):
		filepid = run("sudo cat {}", pidfile, check=False).strip()
		if filepid:
			run("sudo kill {}", filepid, check=False)
		run("sudo rm -f {}", pidfile, check=False)


if __name__ == "__main__":
	main()
