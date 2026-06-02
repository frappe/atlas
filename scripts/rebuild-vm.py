#!/usr/bin/env python3
# Rebuild/Restore a Stopped VM's disk from a source, keeping its identity
# (name, IPv6, MAC, tap, SSH key). The source is either one of the VM's own
# snapshots (Restore) or a base image's pristine rootfs (Rebuild). Either way
# the VM keeps its UUID, so step 2's freshly-derived host keys / machine-id /
# hostname match the VM the operator already knows.
#
# The caller guarantees the VM is Stopped (the unit is down, rootfs unmounted),
# so swapping the file underneath it is safe. firecracker.json, network.env and
# the systemd unit already exist from the original provision and are untouched.
# Idempotent: re-running replaces the rootfs again with the same source.
#
# Successor to rebuild-vm.sh. Same typed Task contract as snapshot-vm.py:
# RebuildInputs.from_args() parses the CLI flags that used to be env vars; there
# is no machine-readable result to emit (the controller only needs the exit
# code), so this prints a human "Rebuilt ..." line like the original.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import VirtualMachinePaths
from atlas.rootfs import Identity, inject_identity, prepare_lv


@dataclass(frozen=True)
class RebuildInputs(TaskInputs):
    """Rebuild/Restore a Stopped VM's disk from a snapshot or a base image,
    keeping the VM's identity."""

    command: typing.ClassVar[str] = "rebuild-vm"
    virtual_machine_name: str  # UUID; locates the VM directory and seeds identity
    disk_gb: int  # target rootfs size (the VM's current disk size)
    virtual_machine_ipv6: str  # injected into the rootfs network env
    ipv4_guest_cidr: str  # guest side of the NAT44 /30, injected into the env
    ipv4_gateway: str  # host side of the /30 (no mask), the guest's v4 gw
    ssh_public_key: str  # injected into authorized_keys
    atlas_fc_uid: int  # per-VM uid; the rebuilt rootfs is chowned back to it
    # Host side of the NAT44 /30. Rebuild does NOT touch network.env (the unit's
    # host-side networking is unchanged), so this is not consumed here — but the
    # controller sends the whole _ipv4_link_variables() dict, shared with provision
    # where host_cidr IS needed. The shell ignored the extra env var for free;
    # argparse is strict and rejects an undeclared flag (exit 2), so we declare it
    # to accept-and-ignore. Optional with a default so a CLI caller can omit it.
    ipv4_host_cidr: str = ""  # accepted for contract parity with provision; unused
    # One source, exactly: a snapshot rootfs path (Restore), OR a base image
    # under /var/lib/atlas/images (Rebuild). The snapshot path wins when set.
    snapshot_rootfs_path: str = ""  # absolute path to a snapshot rootfs (Restore)
    image_name: str = ""  # a base image name (Rebuild)
    rootfs_filename: str = ""  # the image's rootfs file (Rebuild)


def main() -> None:
    inputs = RebuildInputs.from_args()
    pool = ThinPool()
    paths = VirtualMachinePaths(inputs.virtual_machine_name)

    # The disk is the VM's LV; rebuild swaps the LV's contents. The jail node at
    # rootfs.ext4 points at it and is re-created here (the LV's dev_t can change).
    if not os.path.isdir(paths.jail_root):
        sys.exit(f"jail {paths.jail_root} missing; provision the VM before rebuilding")

    # Resolve the origin LV. Snapshot LV wins (Restore); otherwise the base image
    # LV (Rebuild). snapshot_rootfs_path is the snapshot's /dev/atlas/<name> path.
    if inputs.snapshot_rootfs_path:
        origin = pool.from_device(inputs.snapshot_rootfs_path)
        if not origin.exists:
            sys.exit(f"snapshot LV not found: {origin.name} (from {inputs.snapshot_rootfs_path})")
    else:
        if not inputs.image_name:
            sys.exit("image_name required (or pass snapshot_rootfs_path)")
        origin = pool.base_image(inputs.image_name)
        if not origin.exists:
            sys.exit(f"base image LV not present: {origin.name}; run Sync to Server first")

    disk = pool.vm_disk(inputs.virtual_machine_name)

    # Replace the existing disk: drop the old VM LV, then recreate it as a fresh
    # CoW snapshot of the origin. prepare_lv no-ops when the LV exists, so the
    # remove is what forces the swap. remove()'s guard refuses pool/image names;
    # atlas-vm-<uuid> is neither, so this is allowed.
    disk.remove()
    prepare_lv(origin, disk, inputs.disk_gb)
    inject_identity(
        disk.device_path,
        Identity(
            uuid=inputs.virtual_machine_name,
            ipv6_address=inputs.virtual_machine_ipv6,
            ssh_public_key=inputs.ssh_public_key,
            ipv4_guest_cidr=inputs.ipv4_guest_cidr,
            ipv4_gateway=inputs.ipv4_gateway,
        ),
    )

    # Re-mknod the jail node: the new LV's dev_t differs from the old one, so the
    # existing node would point at a stale device. expose_in_jail removes and
    # re-creates it, owned by the per-VM uid (0660) so the jailed FC can open it.
    disk.expose_in_jail(paths.rootfs_node, inputs.atlas_fc_uid)

    print(f"Rebuilt {inputs.virtual_machine_name} from {origin.name}.")


if __name__ == "__main__":
    main()
