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

from atlas.lvm import ThinPool
from atlas.network_env import read_network_env
from atlas.paths import VirtualMachinePaths


def main() -> None:
	if len(sys.argv) != 2:
		sys.exit("usage: vm-disk-up.py <virtual-machine-uuid>")
	uuid = sys.argv[1]

	paths = VirtualMachinePaths(uuid)
	env = read_network_env(paths.network_env)
	uid = env.require_int("ATLAS_FC_UID")

	pool = ThinPool()
	disk = pool.vm_disk(uuid)

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
	data_disk = pool.data_disk(uuid)
	if data_disk.exists:
		data_disk.activate()
		data_disk.expose_in_jail(paths.data_node, uid)


if __name__ == "__main__":
	main()
