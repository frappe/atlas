#!/usr/bin/env python3
# Start a previously provisioned VM. Idempotent (systemd start on a running
# unit is a no-op).
#
# Successor to start-vm.sh. Inputs are parsed once via StartInputs.from_args();
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
class StartInputs(TaskInputs):
    """Start a previously provisioned VM via its systemd unit."""

    command: typing.ClassVar[str] = "start-vm"
    virtual_machine_name: str  # UUID; selects the firecracker-vm@<uuid> instance


def main() -> None:
    inputs = StartInputs.from_args()
    paths = VirtualMachinePaths(inputs.virtual_machine_name)

    run("sudo", "systemctl", "start", paths.systemd_unit)
    # is-active confirms the unit actually came up (start returns before the
    # service settles); a failed boot surfaces here as a non-zero Task.
    run("sudo", "systemctl", "is-active", paths.systemd_unit)

    print(f"Started {inputs.virtual_machine_name}.")


if __name__ == "__main__":
    main()
