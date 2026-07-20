#!/usr/bin/env python3
# Wake a sleeping VM: remove the SLEEPING marker (so host reboots will auto-start
# it again), then start the systemd unit. If a memory snapshot is present (the
# READY marker exists), the launcher resumes the guest in milliseconds; otherwise
# it cold-boots. Either way, when this script exits the unit has been started.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run
from atlas._task import TaskInputs
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class WakeVmInputs(TaskInputs):
	"""Remove the SLEEPING marker and start the VM's unit."""

	command: typing.ClassVar[str] = "wake-vm"
	virtual_machine_name: str


def main() -> None:
	inputs = WakeVmInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	# Remove the marker BEFORE starting the unit: if the unit's ConditionPathNotExists
	# sees the marker it will silently refuse to start (exit 0, unit skipped).
	# Remove first so a restart after a failed start also clears it.
	run("sudo rm -f {}", paths.sleeping_marker)
	run("sudo systemctl start {}", paths.systemd_unit)
	print(f"VM {inputs.virtual_machine_name} woken.")


if __name__ == "__main__":
	main()
