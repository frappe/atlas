"""Per-VM rootfs preparation — the successor to scripts/lib/prepare-rootfs.sh.

Shared by provision and rebuild: create a per-VM rootfs LV from a source (the
read-only base image LV, or a snapshot LV for clone/restore), grow it, give it a
fresh ext4 UUID, and inject this VM's identity (SSH key, network env, hostname,
swap, fresh host keys, machine-id). Each VM gets unique identity even when the
source blocks came from another VM's snapshot, because host keys and machine-id
are rewritten here from this VM's UUID.

The shell version used a `trap ... EXIT` to guarantee the mount is torn down on
any failure. Here that is a context manager (`_mounted`) — a try/finally the
type checker can see, instead of a trap a reader has to remember is armed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from atlas._run import install_directory, install_file, run, run_input
from atlas.lvm import LogicalVolume


@dataclass(frozen=True)
class Identity:
    """The per-VM identity injected into a freshly-prepared rootfs. Typed so a
    caller can't transpose the IPv6 and the SSH key (both strings) by position."""

    uuid: str
    ipv6_address: str
    ssh_public_key: str
    ipv4_guest_cidr: str
    ipv4_gateway: str

    @property
    def hostname(self) -> str:
        """First 8 chars of the UUID — enough to recognize the VM in prompts and
        journal lines. The 127.0.1.1 entry is the Debian `hostname -f` convention."""
        return f"atlas-{self.uuid[:8]}"

    @property
    def machine_id(self) -> str:
        """32 lowercase hex chars derived from the UUID: stable across this VM's
        reboots, unique across VMs."""
        return self.uuid.replace("-", "")[:32]


def prepare_lv(origin: LogicalVolume, target: LogicalVolume, disk_gigabytes: int) -> LogicalVolume:
    """Create `target` as a CoW thin snapshot of `origin`, grow it to
    disk_gigabytes if larger, give it a fresh ext4 UUID + label, leave it
    activated. Idempotent: snapshot_into no-ops (and re-activates) if `target`
    already exists, so a re-provision reuses the same disk.

    A CoW snapshot inherits the origin's ext4 UUID; `tune2fs -U random` gives
    each per-VM disk a distinct UUID so host-side blkid stays honest (the guest
    mounts root=/dev/vda, UUID-agnostic). Done while unmounted.
    """
    origin.snapshot_into(target)
    device = target.device_path
    # Grow to the VM's size if larger than the origin. -r resizes the fs in the
    # same shot; a no-op when sizes already match, so guard on it failing-clean.
    run("sudo", "lvextend", "-r", "-L", f"{disk_gigabytes}G", device, check=False, quiet=True)
    run("sudo", "e2fsck", "-fy", device, check=False, quiet=True)
    run("sudo", "tune2fs", "-U", "random", "-L", "atlas-root", device)
    return target


def inject_identity(device: str, identity: Identity) -> None:
    """Mount `device` and write this VM's identity into it: authorized_keys, the
    network env (IPv6 + the private IPv4 egress link), hostname + hosts entry, a
    512 MiB swapfile, fresh SSH host keys, a UUID-derived machine-id. Unmounts on
    return and on error (the context manager guarantees it)."""
    with _mounted(device) as mount_point:
        _write_authorized_keys(mount_point, identity.ssh_public_key)
        _write_network_env(mount_point, identity)
        _write_hostname(mount_point, identity.hostname)
        _write_swapfile(mount_point)
        _regenerate_host_keys(mount_point, identity.hostname)
        _write_machine_id(mount_point, identity.machine_id)


@contextmanager
def _mounted(device: str):
    """Mount `device` on a fresh temp dir; unmount + rmdir on exit, success or
    failure. The LV is a block device — mount it directly, no `-o loop`. Replaces
    the shell `trap ... EXIT`."""
    mount_point = run("sudo", "mktemp", "-d", "/tmp/atlas-mount-XXXXXX").strip()
    run("sudo", "mount", device, mount_point)
    try:
        yield mount_point
    finally:
        run("sudo", "umount", mount_point, check=False, quiet=True)
        run("sudo", "rmdir", mount_point, check=False, quiet=True)


def _write_authorized_keys(mount_point: str, ssh_public_key: str) -> None:
    install_directory(f"{mount_point}/root/.ssh", mode="0700")
    install_file(ssh_public_key + "\n", f"{mount_point}/root/.ssh/authorized_keys", mode="0600")


def _write_network_env(mount_point: str, identity: Identity) -> None:
    content = (
        f"VIRTUAL_MACHINE_IPV6={identity.ipv6_address}\n"
        f"VIRTUAL_MACHINE_IPV4={identity.ipv4_guest_cidr}\n"
        f"VIRTUAL_MACHINE_IPV4_GATEWAY={identity.ipv4_gateway}\n"
    )
    install_file(content, f"{mount_point}/etc/atlas-network.env", mode="0644")


def _write_hostname(mount_point: str, hostname: str) -> None:
    install_file(hostname + "\n", f"{mount_point}/etc/hostname", mode="0644")
    # Append the 127.0.1.1 mapping `hostname -f` resolves against. `tee -a`
    # writes the file and echoes to stdout; route the echo to a throwaway via
    # `sh -c` so it never pollutes a task's parsed stdout.
    run_input("sudo", "sh", "-c", f"tee -a {hostname_hosts_path(mount_point)} >/dev/null",
              stdin=f"\n127.0.1.1\t{hostname}\n")


def hostname_hosts_path(mount_point: str) -> str:
    return f"{mount_point}/etc/hosts"


def _write_swapfile(mount_point: str) -> None:
    # 512 MiB keeps small apt installs from OOMing; lands at /swapfile, picked up
    # by the fstab from sync-image.
    swapfile = f"{mount_point}/swapfile"
    run("sudo", "dd", "if=/dev/zero", f"of={swapfile}", "bs=1M", "count=512", "status=none")
    run("sudo", "chmod", "0600", swapfile)
    run("sudo", "mkswap", swapfile, quiet=True)


def _regenerate_host_keys(mount_point: str, hostname: str) -> None:
    # The CI rootfs has no first-boot keygen, so sshd dies without keys; generate
    # per-VM keys here. On a snapshot/clone source this also overwrites the source
    # VM's keys so the new VM is not a duplicate.
    install_directory(f"{mount_point}/etc/ssh", mode="0755")
    for key_type in ("rsa", "ecdsa", "ed25519"):
        key_path = f"{mount_point}/etc/ssh/ssh_host_{key_type}_key"
        run("sudo", "rm", "-f", key_path, f"{key_path}.pub")
        run("sudo", "ssh-keygen", "-q", "-t", key_type, "-f", key_path, "-N", "",
            "-C", f"root@{hostname}")


def _write_machine_id(mount_point: str, machine_id: str) -> None:
    install_file(machine_id + "\n", f"{mount_point}/etc/machine-id", mode="0444")
