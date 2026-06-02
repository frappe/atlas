#!/usr/bin/env python3
# Resume a Paused VM: unfreeze its vCPUs via Firecracker's API socket.
# Idempotent: resuming an already-running microVM is ignored by Firecracker
# (returns 2xx).
#
# Successor to resume-vm.sh. Pure host op over the jailed API socket — no LVM, no
# disk touch. ResumeInputs.from_args() parses the one CLI flag; the state change
# either succeeds (2xx) or surfaces as a failed Task (curl --fail). No result
# line — the controller only needs the exit code.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import firecracker_api_patch
from atlas._task import TaskInputs
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class ResumeInputs(TaskInputs):
    """Resume a Paused VM by unfreezing its vCPUs via the Firecracker API socket."""

    command: typing.ClassVar[str] = "resume-vm"
    virtual_machine_name: str  # UUID; selects the API socket


def main() -> None:
    inputs = ResumeInputs.from_args()
    paths = VirtualMachinePaths(inputs.virtual_machine_name)

    # The API socket is created by Firecracker inside its jail (a host-filesystem
    # unix socket; the VM's network namespace doesn't affect reaching it). The
    # jail path nests the UUID twice, exceeding the 108-byte sun_path limit, so we
    # connect via a SHORT relative name: the existence test uses the absolute path
    # (stat() has no length limit), while the PATCH cd-s into the socket directory
    # and addresses it as just firecracker.socket. See pause-vm.py / paths.py for
    # the full rationale.
    if not os.path.exists(paths.api_socket):
        sys.exit(f"API socket {paths.api_socket} not present; is the VM running?")

    firecracker_api_patch(
        paths.api_socket_directory,
        paths.api_socket_name,
        '{"state": "Resumed"}',
    )

    print(f"Resumed {inputs.virtual_machine_name}.")


if __name__ == "__main__":
    main()
