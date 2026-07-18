#!/usr/bin/env python3
# Drive + observe dm-clone hydration for a migrating VM. Called once per scheduler
# tick by the Hydrating phase: it enables hydration on first call (idempotent —
# enabling an already-hydrating device is a no-op) and reports the current percent.
# The CONTROLLER, not this script, decides when to advance — keeping the
# multi-minute copy off the worker as a series of cheap read-only probes (spec/24).
#
# SAMPLE / ILLUSTRATIVE. Emits ATLAS_RESULT={"hydration_percent": N}.
#
# dm-clone status line looks like:
#   0 <sectors> clone <meta_used>/<meta_total> <region_size> <hydrated>/<total> ...
# the hydrated/total pair (in regions) gives the percent.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult

CLONE_DEV = "atlas-vm-{uuid}-clone"


@dataclass(frozen=True)
class HydrationInputs(TaskInputs):
	"""Enable + poll dm-clone hydration for a migrating VM's disk."""

	command: typing.ClassVar[str] = "migration-poll-hydration"
	virtual_machine_name: str


@dataclass(frozen=True)
class HydrationResult(TaskResult):
	hydration_percent: int


def main() -> None:
	inputs = HydrationInputs.from_args()
	name = CLONE_DEV.format(uuid=inputs.virtual_machine_name)

	if not run_ok("sudo", "dmsetup", "info", name):
		# The device is gone — either never created (controller bug) or already
		# collapsed (cutover happened). Report 100 so the phase advances cleanly on a
		# re-entry after collapse; the cutover phase's own idempotency catches a real
		# missing-device error earlier.
		HydrationResult(hydration_percent=100).emit()
		print(f"dm-clone {name} absent; treating as fully hydrated.")
		return

	# Enable hydration (idempotent). dm-clone copies regions from source→dest in the
	# background once enabled; messaging an already-hydrating device is harmless.
	run("sudo", "dmsetup", "message", name, "0", "enable_hydration")

	percent = _hydration_percent(name)
	HydrationResult(hydration_percent=percent).emit()
	print(f"{name} hydration {percent}%.")


def _hydration_percent(name: str) -> int:
	"""Parse `dmsetup status` for the hydrated/total region pair. Isolated + pure so
	the parse (the bit that breaks on a format change) is unit-testable without a
	dm stack — the same discipline lvm.py uses for lsblk/lvs parsing."""
	status = run("sudo", "dmsetup", "status", name).strip()
	return parse_hydration_percent(status)


def parse_hydration_percent(status_line: str) -> int:
	"""dm-clone status fields (kernel docs):
	  <meta_block> <#used>/<#total_meta> <region_size> <#hydrated>/<#total_regions> ...
	The 5th whitespace field is <#hydrated>/<#total_regions>. 100 when equal."""
	fields = status_line.split()
	# fields[0..2] = start, length, target_type("clone"); the metadata + region
	# accounting begins at fields[3]. The hydrated/total pair is the 2nd "a/b" field.
	pairs = [f for f in fields if "/" in f and f.replace("/", "").isdigit()]
	if len(pairs) < 2:
		raise ValueError(f"cannot parse dm-clone hydration from: {status_line!r}")
	hydrated, total = (int(x) for x in pairs[1].split("/"))
	if total == 0:
		return 100
	return min(100, (hydrated * 100) // total)


if __name__ == "__main__":
	main()
