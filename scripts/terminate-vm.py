#!/usr/bin/env python3
# Delete all on-host state for a VM. Idempotent.
#
# Successor to terminate-vm.sh. The inputs are now a typed CLI:
#   terminate-vm.py --virtual-machine-name <uuid>
# There is no machine-readable result — teardown just tears down — so it prints a
# human "Deleted <uuid>." line like the original, not an ATLAS_RESULT= line.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class TerminateInputs(TaskInputs):
	"""Delete all on-host state for a VM. Idempotent."""

	command: typing.ClassVar[str] = "terminate-vm"
	virtual_machine_name: str  # UUID; identifies the unit, directory, and disk LV


def main() -> None:
	inputs = TerminateInputs.from_args()
	pool = ThinPool()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	# Tolerate failure: the unit may already be gone or never have started.
	run("sudo systemctl disable --now {}", paths.systemd_unit, check=False)

	# In case the unit failed before its ExecStopPost ran, tear down networking
	# explicitly. `boat vm-network-down` is the durable hook (positional uuid) and
	# is itself idempotent — we invoke the same verb the unit's ExecStopPost runs
	# rather than reimplement it.
	if run_ok("sudo test -f {}", paths.network_env):
		# The unit's ExecStopPost runs `/usr/local/bin/boat vm-network-down %i`;
		# invoke the byte-identical verb here so a unit that failed before its own
		# teardown still gets the netns/veth/proxy-NDP swept.
		run(
			"sudo /usr/local/bin/boat vm-network-down {}",
			inputs.virtual_machine_name,
			check=False,
		)

	# Removing the VM directory takes the jail tree (kernel link, config, API
	# socket, and the rootfs.ext4 block NODE) with it — they all live under jail/
	# inside this directory. The node is just a pointer; the LV it points at is a
	# separate object removed next.
	run("sudo rm -rf {}", paths.directory)

	# Boot-then-hydrate leftover (spec/24 §0): a migrated VM ran on a dm-clone that,
	# after collapse, lingers as a linear map onto the plain LV and holds that LV
	# BUSY — the lvremove below would fail "used by another device". The unit is now
	# stopped (fd released), so remove the clone(s) first. No-op for an ordinary VM.
	for suffix in ("", "-data"):
		clone = f"atlas-vm-{inputs.virtual_machine_name}{suffix}-clone"
		if run_ok("sudo dmsetup info {}", clone):
			run("sudo dmsetup remove {}", clone, check=False)

	# Remove the VM's disk LV. LogicalVolume.remove is idempotent (no-op if gone)
	# and guarded: it refuses to remove the thin pool or a base image LV, so a bug
	# that passed a wrong name here can never destroy shared state. The VM's own
	# snapshots (atlas-snap-<snapshot-uuid>) are removed by the per-snapshot
	# delete path (delete-snapshot-vm.py), which the controller cascades on
	# terminate — their names are not derivable from this VM's UUID, so they are
	# NOT removed here.
	pool.vm_disk(inputs.virtual_machine_name).remove()

	# Remove the data disk LV too (the root disk's peer). Idempotent no-op when the
	# VM had none. Like the root disk it lives in the pool, outside the VM
	# directory the rm -rf above cleared, so it must be lvremoved explicitly.
	pool.data_disk(inputs.virtual_machine_name).remove()

	print(f"Deleted {inputs.virtual_machine_name}.")


if __name__ == "__main__":
	main()
