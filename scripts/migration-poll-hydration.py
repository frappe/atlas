#!/usr/bin/env python3
# Drive + observe dm-clone hydration for a migrating VM (spec/19). Called once per
# scheduler tick by the Hydrating phase: it enables hydration on first call
# (idempotent) and reports the current percent. The CONTROLLER decides when to
# advance — keeping the multi-minute copy off the worker as cheap read-only probes.
#
# Emits ATLAS_RESULT={"hydration_percent": N}.
#
# A dm-clone status line looks like:
#   0 <sectors> clone <meta_used>/<meta_total> <region_size> <hydrated>/<total> ...
# the hydrated/total pair (in regions) gives the percent.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult

CLONE_DEV = "atlas-vm-{key}-clone"


@dataclass(frozen=True)
class HydrationInputs(TaskInputs):
	"""Enable + poll dm-clone hydration for a migrating VM's disk(s)."""

	command: typing.ClassVar[str] = "migration-poll-hydration"
	virtual_machine_name: str


@dataclass(frozen=True)
class HydrationResult(TaskResult):
	hydration_percent: int


def main() -> None:
	inputs = HydrationInputs.from_args()
	uuid = inputs.virtual_machine_name

	# Poll every dm-clone device for this VM (root + optional data); report the MIN
	# percent so the phase only advances when BOTH disks are fully hydrated.
	keys = [uuid]
	if run_ok("sudo dmsetup info {}", CLONE_DEV.format(key=uuid + "-data")):
		keys.append(uuid + "-data")

	percents = []
	for key in keys:
		name = CLONE_DEV.format(key=key)
		if not run_ok("sudo dmsetup info {}", name):
			# Device gone — either never created or already collapsed (cutover ran).
			# Treat as fully hydrated so a re-entry after collapse advances cleanly.
			percents.append(100)
			continue
		# Enable hydration (idempotent — messaging an already-hydrating device is
		# harmless). dm-clone copies regions source→dest in the background.
		run("sudo dmsetup message {} 0 enable_hydration", name)
		percents.append(_hydration_percent(name))

	percent = min(percents) if percents else 100
	HydrationResult(hydration_percent=percent).emit()
	print(f"{uuid} hydration {percent}% ({', '.join(keys)}).")


def _hydration_percent(name: str) -> int:
	status = run("sudo dmsetup status {}", name).strip()
	return parse_hydration_percent(status)


def parse_hydration_percent(status_line: str) -> int:
	"""dm-clone status fields (kernel docs):
	  <meta_block> <#used>/<#total_meta> <region_size> <#hydrated>/<#total_regions> ...
	The 2nd "a/b" whitespace field is <#hydrated>/<#total_regions>. 100 when equal.

	Isolated + pure so the parse (the bit that breaks on a format change) is
	unit-testable without a dm stack — the discipline lvm.py uses for lvs parsing."""
	fields = status_line.split()
	pairs = [f for f in fields if "/" in f and f.replace("/", "").isdigit()]
	if len(pairs) < 2:
		raise ValueError(f"cannot parse dm-clone hydration from: {status_line!r}")
	hydrated, total = (int(x) for x in pairs[1].split("/"))
	if total == 0:
		return 100
	return min(100, (hydrated * 100) // total)


if __name__ == "__main__":
	main()
