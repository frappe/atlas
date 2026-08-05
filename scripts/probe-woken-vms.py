#!/usr/bin/env python3
# Probe whether each of the given Sleeping VMs has been WOKEN on the host — i.e.
# atlas-wake-trap.py (or an operator's local wake) removed its `sleeping` marker.
#
# The controller's reconcile_sleeping_vms() calls this per server every minute so a
# host-initiated (packet-triggered) wake is reflected back into the Frappe status
# within one poll cycle: the host is the authority for a wake HAVING happened (only
# it sees the inbound SYN); the controller just mirrors it into the DB. Read-only —
# it changes nothing on the host.

import json
import os
import sys
import typing
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs, TaskResult
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class ProbeWokenVmsInputs(TaskInputs):
	"""Return, per given Sleeping VM uuid, whether the host has woken it."""

	command: typing.ClassVar[str] = "probe-woken-vms"
	vms_json: str  # JSON: ["<uuid>", ...]


@dataclass(frozen=True)
class ProbeWokenVmsResult(TaskResult):
	woken: dict = field(default_factory=dict)  # {"<uuid>": bool}


def main() -> None:
	inputs = ProbeWokenVmsInputs.from_args()
	uuids = json.loads(inputs.vms_json)
	# Woken = the sleeping marker is gone. wake-vm.py and atlas-wake-trap.py both
	# remove it as the FIRST step of a wake (before `systemctl start`), so its
	# absence is the authority that a wake has begun — the same signal the unit's
	# ConditionPathExists=! keys on. A VM whose directory is already gone (a racing
	# terminate) reads as woken too; reconcile only acts on rows still Sleeping.
	woken = {uuid: not os.path.exists(VirtualMachinePaths(uuid).sleeping_marker) for uuid in uuids}
	ProbeWokenVmsResult(woken=woken).emit()


if __name__ == "__main__":
	main()
