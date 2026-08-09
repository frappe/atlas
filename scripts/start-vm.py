#!/usr/bin/env python3
# Start a previously provisioned VM. Idempotent (systemd start on a running
# unit is a no-op).
#
# When snapshot-stop-vm.py left a memory snapshot (READY marker), the unit
# resumes the guest from it — milliseconds instead of a cold boot; the host
# side (launcher + vm-restore.py ExecStartPost) decides that on its own from
# the marker. The one wrinkle this script owns: a FAILED restore consumes the
# marker and fails the start job, after which a relaunch cold-boots — so on
# that exact signature (marker present before, gone after, start failed) retry
# once instead of failing the Task while Restart=always brings the VM up
# behind the controller's back.
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

from atlas._run import CommandError, run, run_ok
from atlas._task import TaskInputs
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class StartInputs(TaskInputs):
	"""Start a previously provisioned VM via its systemd unit."""

	command: typing.ClassVar[str] = "start-vm"
	virtual_machine_name: str  # UUID; selects the firecracker-vm@<uuid> instance


def main() -> None:
	inputs = StartInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	def marker_present() -> bool:
		return run_ok("sudo test -f {}", paths.memory_snapshot_marker)

	restoring = marker_present()
	try:
		run("sudo systemctl start {}", paths.systemd_unit)
	except CommandError:
		# A failed restore consumed the marker and failed the start job, while
		# Restart=always schedules its own relaunch — which would leave the Task
		# Failed and the VM Running behind it. Cancel the pending relaunch and
		# start synchronously: the marker is gone, so this one cold-boots.
		if not (restoring and not marker_present()):
			raise
		run("sudo systemctl reset-failed {}", paths.systemd_unit)
		run("sudo systemctl start {}", paths.systemd_unit)
		restoring = False
	# is-active confirms the unit actually came up (start returns before the
	# service settles); a failed boot surfaces here as a non-zero Task.
	run("sudo systemctl is-active {}", paths.systemd_unit)

	how = "restored from memory snapshot" if restoring else "cold boot"
	print(f"Started {inputs.virtual_machine_name} ({how}).")


if __name__ == "__main__":
	main()
