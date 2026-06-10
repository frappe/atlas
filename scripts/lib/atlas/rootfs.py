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

from atlas._run import install_directory, install_file, run, run_input, run_ok
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
	# Where the data disk is mounted inside the guest. Empty means no data mount
	# (no data disk, or format-and-mount disabled): inject_identity skips the
	# fstab line entirely.
	data_disk_mount_at: str = ""

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


def prepare_data_lv(
	pool, data_lv: LogicalVolume, disk_gigabytes: int, do_format: bool, origin: LogicalVolume | None = None
) -> LogicalVolume:
	"""Bring a per-VM data disk into being and leave it activated. The data-disk
	peer of prepare_lv, with two sources:

	- `origin is None` — a fresh, blank data disk. create_thin it (a private thin
	  volume, no origin). If `do_format`, lay down ext4 labelled `atlas-data` the
	  first time only (a freshly-minted LV); on a later grow, lvextend -r the LV +
	  fs and e2fsck. If not `do_format`, attach the raw block device (grow with a
	  plain lvextend -L, no fs to resize).
	- `origin` set — clone/restore: a CoW thin snapshot of a data-disk snapshot LV,
	  exactly like prepare_lv does for root. Grow to size with -r, then
	  `tune2fs -U random` for a fresh host-side UUID while KEEPING the `atlas-data`
	  label (the guest mounts by LABEL=atlas-data). No mkfs — the data is preserved.

	Idempotent: snapshot_into / create_thin re-activate an existing LV, and the
	grow/e2fsck/tune2fs steps are no-ops once satisfied, so a re-provision reuses
	the same disk without wiping it.
	"""
	if origin is not None:
		origin.snapshot_into(data_lv)
		device = data_lv.device_path
		run("sudo", "lvextend", "-r", "-L", f"{disk_gigabytes}G", device, check=False, quiet=True)
		run("sudo", "e2fsck", "-fy", device, check=False, quiet=True)
		run("sudo", "tune2fs", "-U", "random", "-L", "atlas-data", device)
		return data_lv

	freshly_created = not data_lv.exists
	pool.create_thin(data_lv, disk_gigabytes)
	device = data_lv.device_path
	if do_format:
		if freshly_created:
			# -F: non-interactive even though the device is whole-disk (no partition).
			run("sudo", "mkfs.ext4", "-q", "-L", "atlas-data", "-F", device)
		else:
			run("sudo", "lvextend", "-r", "-L", f"{disk_gigabytes}G", device, check=False, quiet=True)
			run("sudo", "e2fsck", "-fy", device, check=False, quiet=True)
	elif not freshly_created:
		# Raw, unformatted disk: grow the block device only — there is no fs to -r.
		run("sudo", "lvextend", "-L", f"{disk_gigabytes}G", device, check=False, quiet=True)
	return data_lv


def inject_identity(device: str, identity: Identity, *, regenerate_host_keys: bool = False) -> None:
	"""Mount `device` and write this VM's identity into it: authorized_keys, the
	network env (IPv6 + the private IPv4 egress link), hostname + hosts entry, a
	512 MiB swapfile, a UUID-derived machine-id, and the data-disk fstab line.
	Unmounts on return and on error (the context manager guarantees it).

	SSH **host keys** are PRESERVED by default — they are the VM's SSH identity,
	and silently changing them on a rebuild/restore breaks every client's
	known_hosts (looks like a MITM). They are (re)generated only when
	`regenerate_host_keys` is set — provision establishes a fresh identity at
	birth (and must replace the base image's shared baked keys), and the explicit
	Regenerate action rotates them — or when the disk carries none at all (a
	keyless sshd won't start). Rebuild/restore leave them untouched."""
	with _mounted(device) as mount_point:
		_write_authorized_keys(mount_point, identity.ssh_public_key)
		_write_network_env(mount_point, identity)
		_write_hostname(mount_point, identity.hostname)
		_write_swapfile(mount_point)
		_ensure_host_keys(mount_point, identity.hostname, force=regenerate_host_keys)
		_write_machine_id(mount_point, identity.machine_id)
		if identity.data_disk_mount_at:
			_write_data_fstab(mount_point, identity.data_disk_mount_at)


def regenerate_host_keys_on_device(device: str, hostname: str) -> None:
	"""Mount `device` and replace its SSH host keys with fresh ones — the explicit
	'rotate this VM's SSH identity' primitive, the on-demand counterpart to
	inject_identity's preserve-by-default. The caller guarantees the VM is Stopped
	(the rootfs is unmounted in the guest), so mounting it on the host is safe."""
	with _mounted(device) as mount_point:
		_regenerate_host_keys(mount_point, hostname)


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
	run_input(
		"sudo",
		"sh",
		"-c",
		f"tee -a {hostname_hosts_path(mount_point)} >/dev/null",
		stdin=f"\n127.0.1.1\t{hostname}\n",
	)


def hostname_hosts_path(mount_point: str) -> str:
	return f"{mount_point}/etc/hosts"


def _write_swapfile(mount_point: str) -> None:
	# 512 MiB keeps small apt installs from OOMing; lands at /swapfile, picked up
	# by the fstab from sync-image.
	swapfile = f"{mount_point}/swapfile"
	run("sudo", "dd", "if=/dev/zero", f"of={swapfile}", "bs=1M", "count=512", "status=none")
	run("sudo", "chmod", "0600", swapfile)
	run("sudo", "mkswap", swapfile, quiet=True)


def _ensure_host_keys(mount_point: str, hostname: str, *, force: bool) -> None:
	"""Host keys are the VM's SSH identity. Preserve whatever the disk carries so a
	rebuild/restore doesn't change identity out from under clients' known_hosts —
	UNLESS `force` (establish/rotate a fresh identity: provision, or the explicit
	Regenerate action) or the disk has NO host keys (a keyless sshd won't start;
	self-heal). Preserve is the default."""
	if force or not _has_host_keys(mount_point):
		_regenerate_host_keys(mount_point, hostname)


def _has_host_keys(mount_point: str) -> bool:
	"""True if the rootfs already carries an ed25519 host key (the one sshd offers
	by default). `test -f` via sudo because /etc/ssh is root-owned in the mount."""
	return run_ok("sudo", "test", "-f", f"{mount_point}/etc/ssh/ssh_host_ed25519_key")


def _regenerate_host_keys(mount_point: str, hostname: str) -> None:
	# Replace the rootfs's SSH host keys with fresh per-VM ones. Used to establish
	# identity at provision (the base image ships SHARED baked keys that must not
	# be reused across VMs) and to rotate it on demand. NOT called on a plain
	# rebuild/restore — those preserve the disk's keys (see _ensure_host_keys).
	install_directory(f"{mount_point}/etc/ssh", mode="0755")
	for key_type in ("rsa", "ecdsa", "ed25519"):
		key_path = f"{mount_point}/etc/ssh/ssh_host_{key_type}_key"
		run("sudo", "rm", "-f", key_path, f"{key_path}.pub")
		run("sudo", "ssh-keygen", "-q", "-t", key_type, "-f", key_path, "-N", "", "-C", f"root@{hostname}")


def _write_machine_id(mount_point: str, machine_id: str) -> None:
	install_file(machine_id + "\n", f"{mount_point}/etc/machine-id", mode="0444")


def _write_data_fstab(mount_point: str, mount_at: str) -> None:
	"""Mount the data disk at `mount_at` inside the guest. Create the mount dir
	and append an fstab line keyed by `LABEL=atlas-data` (the same LABEL= idiom
	sync-image.py uses for the root fs, so it survives the per-VM tune2fs UUID
	reroll). `nofail` keeps a missing/unformatted data disk from blocking boot.

	Idempotent: skip if an atlas-data line is already present — a rebuild lays
	down a fresh rootfs (image fstab has no such line, so append once), while a
	restore-from-snapshot or re-provision may already carry it (don't duplicate)."""
	fstab = f"{mount_point}/etc/fstab"
	if run_ok("sudo", "grep", "-q", "LABEL=atlas-data", fstab):
		return
	run("sudo", "mkdir", "-p", f"{mount_point}{mount_at}")
	# tee -a appends and echoes; route the echo to /dev/null so it never lands in
	# a Task's parsed stdout (same trick as _write_hostname).
	run_input(
		"sudo",
		"sh",
		"-c",
		f"tee -a {fstab} >/dev/null",
		stdin=f"LABEL=atlas-data\t{mount_at}\text4\tdefaults,nofail\t0\t2\n",
	)
