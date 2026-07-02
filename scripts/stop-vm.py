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

from atlas._run import install_file, run, run_ok
from atlas._task import TaskInputs
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class StopInputs(TaskInputs):
	"""Stop a VM via its systemd unit; ExecStopPost tears down networking."""

	command: typing.ClassVar[str] = "stop-vm"
	virtual_machine_name: str  # UUID; selects the firecracker-vm@<uuid> instance
	# Optional short graceful-stop timeout (seconds). A migration discards the guest's
	# RAM anyway (spec/24 §0.5.2), so waiting out a full shutdown grace period is
	# wasted downtime — a short TimeoutStopSec bounds it. This uses `systemctl stop`
	# with a per-unit runtime override, NOT `systemctl kill -SIGKILL`: the graceful
	# stop still runs ExecStopPost (vm-network-down.py tears down netns/veth/proxy-NDP,
	# which a SIGKILL would skip — leaving the source answering NDP for a /128 that a
	# keep-address forward then collides with). Empty (0) = systemd's default drain.
	stop_timeout_seconds: int = 0


def main() -> None:
	inputs = StopInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	if inputs.stop_timeout_seconds > 0:
		# Bound the graceful drain WITHOUT skipping ExecStopPost. TimeoutStopSec is a
		# load-time property (set-property can't change it at runtime — host-verified),
		# so drop a runtime override under /run and daemon-reload (~0.1s, host-timed).
		# `systemctl stop` still runs ExecStopPost; if the guest doesn't halt in the
		# window systemd SIGKILLs the cgroup and STILL runs ExecStopPost — so
		# vm-network-down.py (netns/veth/proxy-NDP teardown) always fires, which a bare
		# `systemctl kill -SIGKILL` would skip. The override lives in /run (tmpfs), so
		# it evaporates on reboot; we also remove it + reload after the stop so the
		# unit's default drain is restored for an ordinary later stop.
		dropin_dir = f"/run/systemd/system/{paths.systemd_unit}.d"
		dropin = f"{dropin_dir}/atlas-migration-faststop.conf"
		run("sudo mkdir -p {}", dropin_dir)
		install_file(
			f"[Service]\nTimeoutStopSec={inputs.stop_timeout_seconds}s\n",
			dropin,
			mode="0644",
		)
		run("sudo systemctl daemon-reload")
		try:
			run("sudo systemctl stop {}", paths.systemd_unit)
		finally:
			run("sudo rm -f {}", dropin, check=False)
			run("sudo rmdir {}", dropin_dir, check=False, quiet=True)
			run("sudo systemctl daemon-reload", check=False)
	else:
		run("sudo systemctl stop {}", paths.systemd_unit)

	# Boot-then-hydrate convergence (spec/24 §0): once the guest is stopped (its rootfs
	# fd released), tear down any leftover dm-clone device so the disk converges to the
	# plain atlas-vm-<uuid> LV. During a boot-then-hydrate migration the guest ran on the
	# clone (read-through, then collapsed to a linear map onto the plain LV); that clone
	# device lingers and holds the plain LV BUSY — a later lvremove (terminate/rebuild)
	# would fail "used by another device". Removing it here is safe now that nothing has
	# it open, and lets the next start expose the plain LV directly. No-op for an
	# ordinary VM (no clone). Idempotent and best-effort.
	_converge_clone(inputs.virtual_machine_name)

	print(f"Stopped {inputs.virtual_machine_name}.")


def _converge_clone(uuid: str) -> None:
	"""Remove a leftover dm-clone (root and data) so the disk falls back to the plain
	LV. Safe only after the guest is stopped (fd released). Best-effort."""
	for suffix in ("", "-data"):
		name = f"atlas-vm-{uuid}{suffix}-clone"
		if run_ok("sudo dmsetup info {}", name):
			run("sudo dmsetup remove {}", name, check=False)


if __name__ == "__main__":
	main()
