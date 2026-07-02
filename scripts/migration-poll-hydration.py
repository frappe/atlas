#!/usr/bin/env python3
# Drive + observe dm-clone hydration for a migrating VM (spec/24). Called once per
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
	"""Enable + poll dm-clone hydration. Default: a migrating VM's disk(s) (root +
	optional data). With `clone_device` set, poll exactly that one dm device instead
	— used by the local-base-image ship (spec/24), whose clone is keyed by image
	name (atlas-base-<image>-clone), not a VM uuid."""

	command: typing.ClassVar[str] = "migration-poll-hydration"
	virtual_machine_name: str = ""
	clone_device: str = ""


@dataclass(frozen=True)
class HydrationResult(TaskResult):
	hydration_percent: int
	source_healthy: bool = True  # False → the source nbd client died; caller rebuilds


def main() -> None:
	inputs = HydrationInputs.from_args()

	# An explicit clone_device (base-image ship) polls that one device; otherwise
	# poll every VM-disk clone (root + optional data) and report the MIN so the phase
	# only advances when BOTH disks are fully hydrated.
	if inputs.clone_device:
		names = [inputs.clone_device]
		label = inputs.clone_device
	else:
		uuid = inputs.virtual_machine_name
		keys = [uuid]
		if run_ok("sudo dmsetup info {}", CLONE_DEV.format(key=uuid + "-data")):
			keys.append(uuid + "-data")
		names = [CLONE_DEV.format(key=k) for k in keys]
		label = uuid

	percents = []
	healthy = True
	for name in names:
		if not run_ok("sudo dmsetup info {}", name):
			# Device gone — either never created or already collapsed (cutover ran).
			# Treat as fully hydrated so a re-entry after collapse advances cleanly.
			percents.append(100)
			continue
		# A dead source nbd client freezes hydration (reads return 0 bytes) while
		# dmsetup still reports the clone "present" — so we probe the client's
		# liveness, not just the clone's existence. An unhealthy source is reported
		# up so the Hydrating phase re-runs prepare (which rebuilds the stack); we
		# skip enable_hydration/percent on it (both are meaningless on a dead source).
		if not _clone_source_alive(name):
			healthy = False
			continue
		# Enable hydration (idempotent — messaging an already-hydrating device is
		# harmless). dm-clone copies regions source→dest in the background.
		run("sudo dmsetup message {} 0 enable_hydration", name)
		percents.append(_hydration_percent(name))

	percent = min(percents) if percents else (100 if healthy else 0)
	HydrationResult(hydration_percent=percent, source_healthy=healthy).emit()
	print(f"{label} hydration {percent}% healthy={healthy} ({', '.join(names)}).")


NBD_MAJOR = "43"  # Linux block major for /dev/nbd*


def _clone_source_alive(clone_name: str) -> bool:
	"""Whether the nbd client backing this dm-clone is still alive. dm-clone can't be
	asked its source directly, so we read the clone's live table — its 6th field is
	the source device as `major:minor` (dmsetup reports device NUMBERS, not paths,
	e.g. `43:0` for nbd0) — resolve it to /sys/block/nbdN, and check that nbd's owning
	process still exists. `nbd-client -check` botches this: it trusts a stale binding
	whose process has died. A non-nbd source (already collapsed) counts as alive."""
	table = run("sudo dmsetup table {}", clone_name, check=False).strip()
	# "<start> <len> clone <meta> <dest> <SOURCE> <region> ..." — source is field 5.
	fields = table.split()
	source = fields[5] if len(fields) > 5 else ""
	if ":" not in source or source.split(":", 1)[0] != NBD_MAJOR:
		return True  # not nbd-backed (e.g. collapsed) — nothing to heal
	# Resolve major:minor → the nbd block device name via sysfs (avoids hardcoding
	# the minor stride), then read that device's recorded client pid.
	block = run("basename $(readlink /sys/dev/block/{})", source, check=False).strip()
	if not block.startswith("nbd"):
		return True
	pid = run("cat /sys/block/{}/pid", block, check=False).strip()
	if not pid.isdigit():
		return False
	return run_ok("test -d /proc/{}", pid)


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
