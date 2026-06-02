#!/usr/bin/env python3
# Pause a Running VM: freeze its vCPUs via Firecracker's API socket. Guest RAM
# stays resident (this is not a shutdown). Idempotent: pausing an already-paused
# microVM keeps it paused (Firecracker returns 2xx either way).
#
# Successor to pause-vm.sh. Pure host op over the jailed API socket — no LVM, no
# disk touch. PauseInputs.from_args() parses the one CLI flag; the state change
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
class PauseInputs(TaskInputs):
    """Pause a Running VM by freezing its vCPUs via the Firecracker API socket."""

    command: typing.ClassVar[str] = "pause-vm"
    virtual_machine_name: str  # UUID; selects the API socket


def main() -> None:
    inputs = PauseInputs.from_args()
    paths = VirtualMachinePaths(inputs.virtual_machine_name)

    # The API socket is created by Firecracker inside its jail. It is a
    # unix-domain socket on the host filesystem; the VM's network namespace does
    # not affect reaching it. BUT the jail path nests the UUID twice
    # (.../<uuid>/jail/firecracker/<uuid>/root/run/firecracker.socket) — ~115
    # chars, past the 108-byte sun_path limit, so curl --unix-socket with the
    # absolute path fails "Unix socket path too long". The existence test below
    # uses the absolute path (stat() has no length limit); the PATCH connects via
    # the SHORT relative name after cd-ing into the socket directory — see
    # firecracker_api_patch().
    if not os.path.exists(paths.api_socket):
        sys.exit(f"API socket {paths.api_socket} not present; is the VM running?")

    firecracker_api_patch(
        paths.api_socket_directory,
        paths.api_socket_name,
        '{"state": "Paused"}',
    )

    print(f"Paused {inputs.virtual_machine_name}.")


if __name__ == "__main__":
    main()
