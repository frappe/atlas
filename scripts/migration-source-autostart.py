#!/usr/bin/env python3
# Source side of a VM migration (spec/24 §3), PENDING phase: take the source VM's
# systemd unit out of multi-user.target for the duration of the move — and put it
# back if the move is abandoned.
#
# The migration's core invariant is that the source guest stays Stopped from
# `Pending` until `Cleanup` (spec/24 §0.3): that is what makes the target's
# read-through consistent and the rollback trivial. `systemctl stop` alone does
# NOT give that invariant. provision-vm.py enables the unit, it carries
# `[Install] WantedBy=multi-user.target`, and its only condition is the SLEEPING
# marker — there is no migration condition. So until `Cleanup`'s
# `disable --now` (the LAST phase, potentially hours later) a source-host reboot
# cold-boots a SECOND live copy of the guest: same UUID, same UUID-derived MAC and
# tap, same host keys, and on a keep-address migration the same public /128
# answered by two hosts. Nothing is asked — systemd's multi-user.target.wants
# symlink starts it.
#
# `disable` (never `disable --now`, never `mask`) is deliberately the weakest thing
# that closes that hole. It removes the WantedBy symlink and touches nothing else:
# a running unit keeps running, and an explicit `systemctl start` — the spec/24 §3
# rollback, "just restarts the intact source VM" — still works. A marker file plus
# `ConditionPathExists=!` would block that explicit start too, which is the failure
# mode sleepy VMs already paid for.
#
# Idempotent: enabling an enabled unit and disabling a disabled one are both no-ops.
#
# Inputs:
#   virtual_machine_name  - UUID
#   enabled               - 0 (the default, what `Pending` sends) removes the unit
#                           from multi-user.target; 1 restores it, the inverse an
#                           operator runs to hand an abandoned source copy back its
#                           reboot survival. int, not bool: the task-input parser
#                           only special-cases int, and a bool field would arrive
#                           as the string "0" (truthy).

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run
from atlas._task import TaskInputs
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class SourceAutostartInputs(TaskInputs):
	"""Set whether the source VM's unit starts itself on the next host reboot."""

	command: typing.ClassVar[str] = "migration-source-autostart"
	virtual_machine_name: str
	enabled: int = 0


def main() -> None:
	inputs = SourceAutostartInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	verb = "enable" if inputs.enabled else "disable"
	run("sudo systemctl {} {}", verb, paths.systemd_unit)

	print(f"Autostart {verb}d for {paths.systemd_unit}.")


if __name__ == "__main__":
	main()
