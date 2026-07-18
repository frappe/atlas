#!/usr/bin/env python3
# Target side of a VM migration (spec/24), CLONE-COLLAPSE step: collapse the
# fully-hydrated dm-clone device(s) once every block is local. In boot-then-hydrate
# (spec/24 §0) the guest is ALREADY LIVE on the clone when this runs, holding the
# rootfs fd open, so we CANNOT `dmsetup remove` the clone — that fails "Device or
# resource busy" (host-verified on real f1 thin LVs, 2026-07-02). Instead we collapse
# TRANSPARENTLY: suspend the clone, reload its table from `clone` to a `linear`
# mapping straight onto the fully-hydrated dest LV, and resume. The dm device keeps
# the SAME major:minor, so Firecracker's open fd stays valid — no re-mknod, no drive
# re-open, no unit blip. The clone then serves the pure-local dest with zero
# read-through; the source NBD client is disconnected.
#
# Once the VM is later stopped and the fd released, `dmsetup remove` on the linear
# node succeeds and the plain `atlas-vm-<uuid>` LV directly holds every block, so the
# next provision re-exposes the plain LV with no clone in the way (steady state
# converges on its own).
#
# Idempotent: a clone already carrying a `linear` table (a re-entry after collapse)
# is a no-op; a missing clone device (already removed, e.g. a cold-path re-entry) is
# a no-op. Guards that hydration is 100% before collapsing (collapsing a partially
# hydrated clone would strand un-copied blocks behind a torn-down NBD).
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
	"""Collapse a migrated VM's hydrated dm-clone(s) transparently to a linear map
	onto the plain thin LV (the guest is live on the clone)."""

	command: typing.ClassVar[str] = "migration-cutover-target"
	virtual_machine_name: str
	data_disk_gb: int = 0
	nbd_base_slot: int = 0  # must match clone-target's, so we free the RIGHT nbd devices


def main() -> None:
	inputs = CutoverInputs.from_args()
	uuid = inputs.virtual_machine_name

	# root = base+0, data = base+1 — the same per-VM block clone-target attached.
	keys = [(uuid, inputs.nbd_base_slot)]
	if inputs.data_disk_gb > 0:
		keys.append((uuid + "-data", inputs.nbd_base_slot + 1))

	for key, nbd_slot in keys:
		_collapse(key, nbd_slot)

	print(f"Collapsed dm-clone(s) for {uuid} to linear; disk is pure-local (fd preserved).")


def _collapse(key: str, nbd_slot: int) -> None:
	name = CLONE_DEV.format(key=key)
	if not run_ok("sudo dmsetup info {}", name):
		# No clone device — a cold-path re-entry where the disk is already the plain
		# LV, or a prior collapse that also removed the node. Nothing to collapse;
		# just make sure the nbd client and clone-meta LV are gone so a re-run
		# fully converges.
		_disconnect_nbd(nbd_slot)
		_remove_meta(key)
		return

	table = run("sudo dmsetup table {}", name).strip()
	if _is_linear(table):
		# Already collapsed (idempotent re-entry). The clone is serving the dest
		# linearly; still converge the nbd client / meta LV teardown.
		_disconnect_nbd(nbd_slot)
		_remove_meta(key)
		return

	# Guard: only collapse a fully-hydrated device. dm-clone status field pair 2 is
	# <hydrated>/<total>; refuse if not equal (the controller only calls us at 100%,
	# but a lost-task re-entry could arrive early). Collapsing early would map the
	# linear dest before every region is copied → holes reading as zeros.
	status = run("sudo dmsetup status {}", name).strip()
	if not _fully_hydrated(status):
		sys.exit(f"refusing to collapse {name}: not fully hydrated ({status!r})")

	# TRANSPARENT collapse: swap the clone table for a linear map onto the dest LV,
	# keeping the SAME dm device so the guest's open rootfs fd survives. The dest LV
	# is the clone's write target (atlas-vm-<key>) and now holds every hydrated block.
	dest_device = f"/dev/atlas/atlas-vm-{key}"
	sectors = _clone_sectors(table)
	run("sudo dmsetup suspend {}", name)
	run("sudo dmsetup reload {} --table {}", name, f"0 {sectors} linear {dest_device} 0")
	run("sudo dmsetup resume {}", name)

	# The linear map no longer reads through NBD; disconnect the source client and
	# drop the now-unused clone metadata LV.
	_disconnect_nbd(nbd_slot)
	_remove_meta(key)


def _is_linear(table_line: str) -> bool:
	"""True if the dm table is already a `linear` target (a collapsed clone)."""
	fields = table_line.split()
	return len(fields) >= 3 and fields[2] == "linear"


def _clone_sectors(table_line: str) -> int:
	"""Total sector count from a dm-clone table line: `0 <sectors> clone ...`."""
	return int(table_line.split()[1])


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
