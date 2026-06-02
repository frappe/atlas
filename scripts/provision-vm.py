#!/usr/bin/env python3
# Provision one Firecracker VM on this server. Single task: prepares disk,
# config, networking, then starts the systemd unit. Run once per VM.
#
# Successor to provision-vm.sh, the biggest task. Same typed Task contract as
# snapshot-vm.py / rebuild-vm.py: ProvisionInputs.from_args() parses the CLI
# flags that used to be env vars; there is no machine-readable result to emit
# (the controller only needs the exit code), so this prints a human
# "Provisioned ..." line like the original.
#
# Inputs (formerly environment variables, now --kebab-case CLI flags):
#   virtual_machine_name  - UUID, used for directory, tap, systemd instance
#   image_name            - directory under /var/lib/atlas/images
#   kernel_filename       - filename inside the image directory
#   rootfs_filename       - filename inside the image directory
#   vcpus                 - integer
#   memory_mb             - integer
#   disk_gb               - integer, final rootfs size for this VM
#   mac_address           - e.g. 06:00:01:02:03:04
#   tap_device            - e.g. atlas-<first 9 hex of vm name>
#   virtual_machine_ipv6  - the VM's address inside the server's /124
#   ipv4_host_cidr        - host side of the per-VM NAT44 /30, e.g. 100.64.0.9/30
#   ipv4_guest_cidr       - guest side of the same /30, e.g. 100.64.0.10/30
#   ipv4_gateway          - host side address (no mask), the guest's v4 gateway
#   ssh_public_key        - injected into the rootfs
#   atlas_fc_uid          - per-VM uid the jailer drops Firecracker to (gid == uid)
#   atlas_netns           - per-VM network namespace name
#   host_veth             - host-side veth interface name
#   namespace_veth        - namespace-side veth interface name
#   cgroup_arg (repeatable)   - one cgroup VALUE per flag, e.g.
#                               --cgroup-arg memory.max=… --cgroup-arg "cpu.max=Q P".
#                               The launcher prefixes each with --cgroup (see NOTE).
#   resource_arg (repeatable) - one resource-limit VALUE per flag; the launcher
#                               prefixes each with --resource-limit.
#   snapshot_rootfs_path  - optional; a snapshot's /dev/atlas/<name> device path
#                           (clone path); empty for a base-image provision

import glob
import json
import os
import shlex
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, install_file, run
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import VirtualMachinePaths, image_directory
from atlas.rootfs import Identity, inject_identity, prepare_lv


@dataclass(frozen=True)
class ProvisionInputs(TaskInputs):
    """Provision one Firecracker VM on this server: disk, config, networking,
    then start the systemd unit."""

    command: typing.ClassVar[str] = "provision-vm"
    virtual_machine_name: str  # UUID; directory, tap, systemd instance, identity
    image_name: str  # directory under /var/lib/atlas/images
    kernel_filename: str  # filename inside the image directory
    rootfs_filename: str  # filename inside the image directory
    vcpus: int
    memory_mb: int
    disk_gb: int  # final rootfs size for this VM
    mac_address: str
    tap_device: str
    virtual_machine_ipv6: str  # the VM's address inside the server's /124
    ipv4_host_cidr: str  # host side of the per-VM NAT44 /30
    ipv4_guest_cidr: str  # guest side of the same /30
    ipv4_gateway: str  # host side address (no mask), the guest's v4 gateway
    ssh_public_key: str  # injected into the rootfs
    atlas_fc_uid: int  # per-VM uid the jailer drops Firecracker to (gid == uid)
    atlas_netns: str  # per-VM network namespace name
    host_veth: str  # host-side veth interface name
    namespace_veth: str  # namespace-side veth interface name
    # Jailer cgroup/resource limits as REPEATABLE VALUE flags: --cgroup-arg
    # memory.max=… --cgroup-arg "cpu.max=100000 100000". Each flag carries the
    # VALUE only; _jailer_launch prefixes each with --cgroup / --resource-limit.
    #
    # Values-only (not the interleaved "--cgroup <v> --cgroup <v>" the shell's
    # ATLAS_CGROUP_ARGS held) for two reasons: a literal "--cgroup" token in an
    # append-list collides with argparse's flag parsing, and the prefix is a
    # constant the launcher owns anyway. This still kills the shell's mapfile
    # hack — a value with an internal space (cpu.max's "<quota> <period>") is one
    # argv token, no word-splitting. TaskInputs renders a list field as append.
    cgroup_arg: list  # cgroup VALUES; launcher emits `--cgroup <value>` per item
    resource_arg: list  # resource-limit VALUES; launcher emits `--resource-limit <value>`
    # One optional source override: a snapshot rootfs path (clone path). Empty
    # means provision from the base image. Mirrors the shell's "${VAR:-}".
    snapshot_rootfs_path: str = ""  # a snapshot's /dev/atlas/<name> device path


def main() -> None:
    inputs = ProvisionInputs.from_args()
    pool = ThinPool()
    paths = VirtualMachinePaths(inputs.virtual_machine_name)
    image = image_directory(inputs.image_name)
    uid = inputs.atlas_fc_uid

    # 0. Verify image present. Fail loud with an actionable message so the
    #    operator knows to click Sync to Server before retrying. (Image sync is
    #    multi-minute and is intentionally not auto-triggered from provision.)
    #    The kernel is needed regardless of the rootfs source, so this probe
    #    stays even when the rootfs comes from a snapshot (clone path,
    #    snapshot_rootfs_path set).
    rootfs_image = f"{image}/{inputs.rootfs_filename}"
    if not os.path.isfile(rootfs_image):
        sys.exit(
            f"image '{inputs.image_name}' not present on server (missing "
            f"{rootfs_image}); run Sync to Server first"
        )

    # 0b. Per-VM uid collision guard. The uid is derived from the UUID and is
    #     almost always unique, but a mod collision is possible. If a *different*
    #     live VM's jail rootfs is already owned by this uid, fail loud rather
    #     than silently letting two VMs share a uid (which would break inter-jail
    #     isolation).
    _guard_uid_collision(uid, paths.rootfs_node)

    install_directory(paths.directory, mode="0700")
    install_directory(paths.log_directory, mode="0700")
    install_directory(paths.jail_root, mode="0700")
    install_directory(paths.api_socket_directory, mode="0700")

    # 1. Per-VM disk LV. An instant CoW thin snapshot of an origin LV — the
    #    pristine image's base LV normally, or a snapshot LV when cloning
    #    (snapshot_rootfs_path is that snapshot's /dev/atlas/<name> device path).
    #    No full copy: unwritten blocks are shared with the origin. The per-VM
    #    identity injected in step 2 is freshly derived from THIS VM's UUID, so a
    #    clone never shares host keys or machine-id with its source. Same origin
    #    resolution and guards as rebuild.
    origin = _resolve_origin(inputs, pool)
    disk = pool.vm_disk(inputs.virtual_machine_name)
    prepare_lv(origin, disk, inputs.disk_gb)

    # 2. Inject this VM's identity (SSH key, network env, hostname, swap, host
    #    keys, machine-id) into the disk. Mounts the LV device directly (no loop).
    #    The v4 egress link goes into the guest's network env here too, so
    #    clone/rebuild get it for free. Done outside the jail, before the jailer
    #    starts.
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

    # 3. Kernel inside the jail. Hard-link (not copy) the immutable image kernel
    #    so we don't duplicate it per VM; same filesystem (/var/lib/atlas), so
    #    the link always succeeds. Read-only is fine for the jailed process.
    run("sudo", "ln", "-f", f"{image}/{inputs.kernel_filename}", paths.kernel)

    # 4. Firecracker config inside the jail, with jail-RELATIVE paths — they are
    #    resolved by the jailed process after chroot, so they are relative to the
    #    jail root (rootfs.ext4, vmlinux), not absolute host paths.
    install_file(_firecracker_config(inputs), paths.firecracker_config, mode="0644")

    # 4b. Expose the disk LV inside the jail as a block-special node at
    #     rootfs.ext4. firecracker.json's jail-relative `path_on_host:
    #     "rootfs.ext4"` (step 4) resolves to this node post-chroot — FC opens it
    #     as a plain block device, no config change from the file-backed era. The
    #     node is owned by the per-VM uid (chmod 0660); device access is pure
    #     DAC. The jailer never deletes existing nodes, so it survives every
    #     (re)start.
    disk.expose_in_jail(paths.rootfs_node, uid)

    # 5. Hand the jail tree to the per-VM uid/gid. The jailer also chowns the
    #    jail root and the device nodes it creates, but the backing files we laid
    #    down (kernel RO, config) must be owned by the uid too. The recursive
    #    chown re-touches the rootfs.ext4 block node's inode (already uid-owned
    #    from step 4b) — correct and harmless; it chowns the node, not the LV it
    #    points at. Do this last, after every file is in place.
    run("sudo", "chown", "-R", f"{uid}:{uid}", paths.jail_chroot_base)

    # 6. Sidecar that vm-network-up.sh reads. Stable across host reboots —
    #    carries the tap, address, and the per-VM netns + veth names so
    #    networking is reconstructible after a host reboot without consulting the
    #    Frappe DB.
    install_file(_network_env(inputs), paths.network_env, mode="0644")

    # 7. Per-VM launcher the systemd unit execs. We build the jailer command line
    #    HERE rather than inline in the unit's ExecStart because the --cgroup
    #    cpu.max value is "<quota> <period>" (an internal space the cgroup file
    #    format requires). systemd word-splits an unquoted $VAR in ExecStart on
    #    every space, which would shatter that value into a stray positional the
    #    jailer rejects ("Found argument '100000' ..."). The launcher is
    #    regenerated on every (re)provision, so it stays in sync with the row.
    #    `exec` so the jailer is the unit's main PID (KillMode=mixed). With a real
    #    arg vector the mapfile dance is gone: each cgroup/resource token is one
    #    pre-quoted line in the exec, internal spaces intact.
    install_file(_jailer_launch(inputs, paths), paths.jailer_launch, mode="0755")

    # 8. Enable and start the systemd unit.
    run("sudo", "systemctl", "enable", "--now", paths.systemd_unit)

    print(f"Provisioned {inputs.virtual_machine_name}.")


def _guard_uid_collision(uid: int, own_rootfs_node: str) -> None:
    """Fail loud if a *different* live VM's jail rootfs is already owned by this
    uid. Ports the shell's glob-and-stat loop. Our own node (idempotent re-run)
    is skipped. `stat -c %u` reads the owning uid; sudo because the jail tree is
    0700-owned by per-VM uids."""
    pattern = "/var/lib/atlas/virtual-machines/*/jail/firecracker/*/root/rootfs.ext4"
    for other_jail in glob.glob(pattern):
        if other_jail == own_rootfs_node:  # our own (idempotent re-run)
            continue
        owner = run("sudo", "stat", "-c", "%u", other_jail).strip()
        if owner == str(uid):
            sys.exit(
                f"uid {uid} already owned by {other_jail}; uid collision — "
                f"terminate that VM or re-roll"
            )


def _resolve_origin(inputs: "ProvisionInputs", pool: ThinPool):
    """Resolve the origin LV the per-VM disk snapshots from. A snapshot LV wins
    (clone path); otherwise the base image LV. Same guards as rebuild —
    snapshot_rootfs_path is the snapshot's /dev/atlas/<name> device path."""
    if inputs.snapshot_rootfs_path:
        origin = pool.from_device(inputs.snapshot_rootfs_path)
        if not origin.exists:
            sys.exit(
                f"snapshot LV not found: {origin.name} "
                f"(from {inputs.snapshot_rootfs_path})"
            )
    else:
        origin = pool.base_image(inputs.image_name)
        if not origin.exists:
            sys.exit(f"base image LV not found: {origin.name}; run Sync to Server first")
    return origin


def _firecracker_config(inputs: "ProvisionInputs") -> str:
    """The jail's firecracker.json. Built from a dict + json.dumps for
    cleanliness; the boot_args / drives / network-interfaces / machine-config
    shape is identical to the shell heredoc, with jail-RELATIVE host paths
    (vmlinux, rootfs.ext4) resolved post-chroot."""
    config = {
        "boot-source": {
            "kernel_image_path": "vmlinux",
            "boot_args": "console=ttyS0 reboot=k panic=1",
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": "rootfs.ext4",
                "is_root_device": True,
                "is_read_only": False,
            }
        ],
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "guest_mac": inputs.mac_address,
                "host_dev_name": inputs.tap_device,
            }
        ],
        "machine-config": {
            "vcpu_count": inputs.vcpus,
            "mem_size_mib": inputs.memory_mb,
        },
    }
    return json.dumps(config, indent=2) + "\n"


def _network_env(inputs: "ProvisionInputs") -> str:
    """The network.env sidecar vm-network-up.sh reads, byte-shape identical to
    the shell heredoc."""
    return (
        f"TAP_DEVICE={inputs.tap_device}\n"
        f"VIRTUAL_MACHINE_IPV6={inputs.virtual_machine_ipv6}\n"
        f"ATLAS_NETNS={inputs.atlas_netns}\n"
        f"HOST_VETH={inputs.host_veth}\n"
        f"NAMESPACE_VETH={inputs.namespace_veth}\n"
        f"IPV4_HOST_CIDR={inputs.ipv4_host_cidr}\n"
        f"IPV4_GUEST_CIDR={inputs.ipv4_guest_cidr}\n"
        f"ATLAS_FC_UID={inputs.atlas_fc_uid}\n"
    )


def _jailer_launch(inputs: "ProvisionInputs", paths: VirtualMachinePaths) -> str:
    """The per-VM jailer-launch.sh the systemd unit execs. cgroup_args /
    resource_args arrive as lists (one argv token each, from the repeatable
    --cgroup-arg / --resource-arg flags); we expand each as its own
    backslash-continued line in the exec, so a token with an internal space
    (cpu.max's "<quota> <period>") stays one argument — no mapfile, no systemd
    word-splitting.

    CRITICAL: each token MUST be shlex.quote()'d, or bash would word-split
    "cpu.max=100000 100000" back into two arguments in this generated `exec`
    line — re-introducing the exact "Found argument '100000'" bug the shell's
    mapfile + "${cgroup_args[@]}" quoting existed to prevent."""
    jailer_lines = [
        "exec /usr/local/bin/jailer \\",
        f"    --id {inputs.virtual_machine_name} \\",
        "    --exec-file /usr/local/bin/firecracker \\",
        f"    --uid {inputs.atlas_fc_uid} \\",
        f"    --gid {inputs.atlas_fc_uid} \\",
        "    --cgroup-version 2 \\",
        f"    --netns /var/run/netns/{inputs.atlas_netns} \\",
    ]
    # Interleave the constant flag with each value on its own continued line,
    # value shlex.quote()'d so an internal space (cpu.max's "<quota> <period>")
    # survives bash word-splitting — the Python equivalent of the shell's
    # "${cgroup_args[@]}" expansion, with the --cgroup/--resource-limit prefix
    # owned here instead of carried in the input.
    for value in inputs.cgroup_arg:
        jailer_lines.append(f"    --cgroup {shlex.quote(value)} \\")
    for value in inputs.resource_arg:
        jailer_lines.append(f"    --resource-limit {shlex.quote(value)} \\")
    jailer_lines += [
        f"    --chroot-base-dir {paths.jail_chroot_base} \\",
        "    -- \\",
        "    --api-sock run/firecracker.socket \\",
        "    --config-file firecracker.json",
    ]
    exec_block = "\n".join(jailer_lines)
    return (
        "#!/bin/bash\n"
        f"# GENERATED by provision-vm.py for VM {inputs.virtual_machine_name}. "
        "Do not edit.\n"
        "set -euo pipefail\n"
        "\n"
        f"{exec_block}\n"
    )


if __name__ == "__main__":
    main()
