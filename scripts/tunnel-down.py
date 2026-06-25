#!/usr/bin/env python3
# Tear down this Atlas host's WireGuard spoke interface (the tunnel rollback path):
# wg-quick down wg0 + disable wg-quick@wg0, best-effort. Runs on the Atlas host via
# atlas.atlas.local_task.run_local_task; wg-quick / systemctl are sudoers-pinned.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import atlas.tunnel as tunnel
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class TunnelDownInputs(TaskInputs):
	"""Tear down the Atlas spoke interface."""

	command: typing.ClassVar[str] = "tunnel-down"
	interface: str = "wg0"


@dataclass(frozen=True)
class TunnelDownResult(TaskResult):
	interface: str
	down: bool


def main() -> None:
	inputs = TunnelDownInputs.from_args()

	tunnel.down(inputs.interface)

	TunnelDownResult(interface=inputs.interface, down=True).emit()
	print(f"Spoke {inputs.interface} down.")


if __name__ == "__main__":
	main()
