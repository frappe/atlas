#!/usr/bin/env python3
# Host-side disk for a VM. Invoked by ExecStartPre in the systemd unit (must run
# before the jailer's ExecStart so the disk node exists when Firecracker opens
# rootfs.ext4). Reads .../network.env for the per-VM uid. Idempotent — safe to
# re-run on every (re)start.
#
# systemd-invoked, NOT a Task: it takes a single positional argument (the VM
# UUID), not --flags, because the unit's ExecStartPre passes `%i`. It imports the
# DURABLE atlas package under /var/lib/atlas/bin (placed by bootstrap), not the
# per-task staged copy.
#
# Why this exists: the VM disk is a thin snapshot LV. `lvcreate -s` marks it
# activation-skip, so after a host reboot the pool comes up but the disk LV does
# not auto-activate, and its device-mapper minor can renumber. The rootfs.ext4
# block node mknod'd into the jail at provision time then dangles. provision is
# NOT re-run on boot, so without this hook an enabled VM would restart-loop
# against a missing/stale disk. This re-activates the LV (-K overrides the skip)
# and re-mknods the jail node with the LV's current major:minor — the disk
# analogue of vm-network-up.py, reconstructible from on-disk state without the
# Frappe DB.

import os
import sys

# The durable package lives next to this script under /var/lib/atlas/bin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atlas._run import run_ok
from atlas.lvm import ThinPool, expose_device_in_jail
from atlas.network_env import read_network_env
from atlas.paths import VirtualMachinePaths


def _clone_device(uuid: str, suffix: str = "") -> str | None:
	"""The dm-clone read-through device for this VM's disk during a boot-then-hydrate
	migration (spec/24 §0), or None if there is none. `suffix` selects the data-disk
	clone ("-data"). While a migration is booting the guest on the clone (and until
	CollapseClone reloads it to a linear map onto the plain LV), the jail rootfs node
	MUST point at the clone, NOT the plain LV — the plain LV is the clone's hydration
	DEST and is incomplete until 100% (booting it directly serves torn/zero blocks).
	The clone device (whether still `clone` or collapsed to `linear`) is the correct
	view either way."""
	name = f"atlas-vm-{uuid}{suffix}-clone"
	if run_ok("sudo dmsetup info {}", name):
		return f"/dev/mapper/{name}"
	return None


def main() -> None:
	if len(sys.argv) != 2:
		sys.exit("usage: vm-disk-up.py <virtual-machine-uuid>")
	uuid = sys.argv[1]

	paths = VirtualMachinePaths(uuid)
	env = read_network_env(paths.network_env)
	uid = env.require_int("ATLAS_FC_UID")

	pool = ThinPool()
	disk = pool.vm_disk(uuid)

	# Boot-then-hydrate migration (spec/24 §0): if a dm-clone exists for this disk,
	# the guest must read THROUGH it (the plain LV is the hydration dest and is
	# incomplete until collapse). Expose the clone; a restart while the clone lives
	# re-points at the same clone, not the half-baked plain LV. Absent a clone this is
	# an ordinary VM — activate the plain LV and expose it as before.
	root_clone = _clone_device(uuid)
	if root_clone:
		expose_device_in_jail(root_clone, paths.rootfs_node, uid)
	else:
		# Activate the disk LV (-K, so the activation-skip snapshot comes up) and
		# refresh the in-jail block node to the LV's current major:minor. Both are
		# idempotent: a no-reboot restart re-activates an already-active LV (no-op)
		# and re-mknods the same dev_t.
		disk.activate()
		disk.expose_in_jail(paths.rootfs_node, uid)

	# Same dance for the data disk (the root disk's peer) when the VM has one. Its
	# LV is also activation-skip-flagged and its dev_t can renumber across a reboot,
	# so the data.ext4 jail node must be refreshed too or the guest's /dev/vdb would
	# dangle. No-op when the VM has no data disk.
	data_clone = _clone_device(uuid, "-data")
	if data_clone:
		expose_device_in_jail(data_clone, paths.data_node, uid)
	else:
		data_disk = pool.data_disk(uuid)
		if data_disk.exists:
			data_disk.activate()
			data_disk.expose_in_jail(paths.data_node, uid)


if __name__ == "__main__":
	main()
