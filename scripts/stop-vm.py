#!/usr/bin/env python3
# Stop a VM. Networking teardown is fired by the unit's ExecStopPost.
#
# Successor to stop-vm.sh. Inputs are parsed once via StopInputs.from_args();
# the VM is addressed by its per-instance systemd unit (VirtualMachinePaths owns
# the firecracker-vm@<uuid>.service name). No KEY=value result — the controller
# parses nothing back, so this prints a human 'Done' line like the original.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs
from atlas._run import run
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class StopInputs(TaskInputs):
    """Stop a VM via its systemd unit; ExecStopPost tears down networking."""

    command: typing.ClassVar[str] = "stop-vm"
    virtual_machine_name: str  # UUID; selects the firecracker-vm@<uuid> instance


def main() -> None:
    inputs = StopInputs.from_args()
    paths = VirtualMachinePaths(inputs.virtual_machine_name)

    run("sudo", "systemctl", "stop", paths.systemd_unit)

    print(f"Stopped {inputs.virtual_machine_name}.")


if __name__ == "__main__":
    main()
