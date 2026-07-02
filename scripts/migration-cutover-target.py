#!/usr/bin/env python3
# Target side of a VM migration (spec/19), CUTOVER-COLLAPSE step: collapse the
# fully-hydrated dm-clone device(s) down to the plain local thin LV and disconnect
# the nbd client(s). After this the disk is pure-local (no read-through to the
# source), and the controller launches the VM by re-running provision-vm against
# the now-existing disk (preserve_host_keys=1). This script does ONLY the collapse.
#
# Idempotent: if the dm-clone device is already gone, the disk is already collapsed
# — a no-op. Guards that hydration is 100% before collapsing (collapsing a partially
# hydrated device would leave the dest LV with holes reading through a torn-down NBD).
#
# Inputs:
#   virtual_machine_name  - UUID
#   data_disk_gb          - 0 if none (else the data dm-clone is collapsed too)

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs

CLONE_DEV = "atlas-vm-{key}-clone"
CLONE_META = "atlas-clonemeta-{key}"


@dataclass(frozen=True)
class CutoverInputs(TaskInputs):
	"""Collapse a migrated VM's hydrated dm-clone(s) to the plain thin LV."""

	command: typing.ClassVar[str] = "migration-cutover-target"
	virtual_machine_name: str
	data_disk_gb: int = 0


def main() -> None:
	inputs = CutoverInputs.from_args()
	uuid = inputs.virtual_machine_name

	keys = [(uuid, 0)]
	if inputs.data_disk_gb > 0:
		keys.append((uuid + "-data", 1))

	for key, nbd_slot in keys:
		_collapse(key, nbd_slot)

	print(f"Collapsed dm-clone(s) for {uuid}; disk is pure-local.")


def _collapse(key: str, nbd_slot: int) -> None:
	name = CLONE_DEV.format(key=key)
	if not run_ok("sudo dmsetup info {}", name):
		# Already collapsed (idempotent re-entry). Still make sure the nbd client and
		# clone-meta LV are gone so a re-run fully converges.
		_disconnect_nbd(nbd_slot)
		_remove_meta(key)
		return

	# Guard: only collapse a fully-hydrated device. dm-clone status field pair 2 is
	# <hydrated>/<total>; refuse if not equal (the controller only calls us at 100%,
	# but a lost-task re-entry could arrive early).
	status = run("sudo dmsetup status {}", name).strip()
	if not _fully_hydrated(status):
		sys.exit(f"refusing to collapse {name}: not fully hydrated ({status!r})")

	# Collapse: removing the dm-clone mapping leaves the dest thin LV holding every
	# block (all hydrated). The LV name is atlas-vm-<key>, already the real disk.
	run("sudo dmsetup remove {}", name)
	_disconnect_nbd(nbd_slot)
	_remove_meta(key)


def _disconnect_nbd(slot: int) -> None:
	device = f"/dev/nbd{slot}"
	if run_ok("sudo nbd-client -check {}", device):
		run("sudo nbd-client -d {}", device, check=False)


def _remove_meta(key: str) -> None:
	meta = CLONE_META.format(key=key)
	if run_ok("sudo lvs atlas/{}", meta):
		run("sudo lvremove -f atlas/{}", meta, check=False)


def _fully_hydrated(status_line: str) -> bool:
	fields = status_line.split()
	pairs = [f for f in fields if "/" in f and f.replace("/", "").isdigit()]
	if len(pairs) < 2:
		return False
	hydrated, total = (int(x) for x in pairs[1].split("/"))
	return total > 0 and hydrated >= total


if __name__ == "__main__":
	main()
