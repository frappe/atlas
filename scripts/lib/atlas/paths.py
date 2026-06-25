"""On-host path layout — the single source of truth for where a VM's files live.

The jail path nests the VM UUID twice
(.../<uuid>/jail/firecracker/<uuid>/root) because the jailer chroots into
<chroot-base>/firecracker/<id>/root and Atlas points the chroot base at the VM
directory. Six shell scripts (provision, rebuild, resize, pause, resume,
terminate) each rebuilt these paths inline; here they are derived once, typed,
and unit-testable with no host.

`VirtualMachinePaths` also owns the firecracker API-socket workaround: the
absolute socket path exceeds the 108-byte AF_UNIX sun_path limit, so callers
must `cd` into its directory and address it by the short relative name. The
object exposes both halves so a caller never reconstructs that by hand.
"""

from __future__ import annotations

ATLAS_ROOT = "/var/lib/atlas"
IMAGES_DIRECTORY = f"{ATLAS_ROOT}/images"
VIRTUAL_MACHINES_DIRECTORY = f"{ATLAS_ROOT}/virtual-machines"
BIN_DIRECTORY = f"{ATLAS_ROOT}/bin"
# Durable warm-snapshot artifacts (the golden mem/vmstate pair + host
# signature), one directory per Virtual Machine Snapshot row. OUTSIDE any VM
# directory: the golden outlives its build VM and is hard-linked into N clone
# jails, so terminate's rm -rf of a VM tree must never take it.
SNAPSHOTS_DIRECTORY = f"{ATLAS_ROOT}/snapshots"

# AF_UNIX sun_path is 108 bytes including the NUL. The jailed socket's absolute
# path blows past it, which is why the relative-cd dance exists.
SUN_PATH_MAX = 108


class VirtualMachinePaths:
	"""Every on-host path for one VM, derived from its UUID. Pure — no host."""

	def __init__(self, uuid: str):
		self.uuid = uuid

	@property
	def directory(self) -> str:
		"""The VM's root directory; removing it takes the whole jail tree."""
		return f"{VIRTUAL_MACHINES_DIRECTORY}/{self.uuid}"

	@property
	def log_directory(self) -> str:
		return f"{self.directory}/log"

	@property
	def network_env(self) -> str:
		"""Sidecar carrying tap/netns/veth/uid — read by the network + disk
		systemd hooks, reconstructible after a host reboot without the Frappe DB."""
		return f"{self.directory}/network.env"

	@property
	def firewall_env(self) -> str:
		"""Sidecar carrying this VM's public-ingress firewall (spec/20-firewall.md):
		its IPv6 and the allowed proto/port list. Read by vm-network-up.py to re-apply
		the nft public_filter block at cold boot, and written/removed by
		firewall-apply.py. Inside the VM tree so terminate's rm -rf sweeps it. Absent
		for a VM with no firewall — which stays fully public."""
		return f"{self.directory}/firewall.env"

	@property
	def tunnels_directory(self) -> str:
		"""Per-VM directory of WireGuard tunnel sidecars (spec/19-vpn-broker.md).
		vm-network-up.py re-applies every tunnel here at cold boot; vm-tunnel.py
		writes/removes one per request. Inside the VM tree so terminate's rm -rf
		sweeps it with the rest of the VM's host state."""
		return f"{self.directory}/tunnels"

	def tunnel_env(self, tunnel_name: str) -> str:
		"""The KEY=value metadata sidecar for one tunnel (0644)."""
		return f"{self.tunnels_directory}/{tunnel_name}.env"

	def tunnel_key(self, tunnel_name: str) -> str:
		"""The 0600 file holding the tunnel's host private key — `wg set` reads the
		key from a path, never the command line, so the secret stays off the
		process table and out of the Task audit row."""
		return f"{self.tunnels_directory}/{tunnel_name}.key"

	@property
	def jail_chroot_base(self) -> str:
		"""What the jailer's --chroot-base-dir points at."""
		return f"{self.directory}/jail"

	@property
	def jail_root(self) -> str:
		"""The chroot root the jailed Firecracker sees as `/`. The UUID appears
		twice: <dir>/jail / firecracker / <uuid> / root."""
		return f"{self.jail_chroot_base}/firecracker/{self.uuid}/root"

	@property
	def rootfs_node(self) -> str:
		"""The block-special node FC opens as its rootfs, jail-relative `rootfs.ext4`."""
		return f"{self.jail_root}/rootfs.ext4"

	@property
	def data_node(self) -> str:
		"""The block-special node FC opens as the data disk (the guest's /dev/vdb),
		jail-relative `data.ext4` — the peer of rootfs_node. Only present when the
		VM has a data disk."""
		return f"{self.jail_root}/data.ext4"

	@property
	def kernel(self) -> str:
		return f"{self.jail_root}/vmlinux"

	@property
	def firecracker_config(self) -> str:
		return f"{self.jail_root}/firecracker.json"

	@property
	def jailer_launch(self) -> str:
		return f"{self.directory}/jailer-launch.sh"

	@property
	def memory_snapshot_directory(self) -> str:
		"""Where a full memory-state snapshot (vmstate + guest RAM) lands, inside
		the jail so the jailed Firecracker can write it and terminate's rm -rf
		sweeps it. Written by snapshot-stop-vm.py, consumed by vm-restore.py."""
		return f"{self.jail_root}/snapshot"

	@property
	def memory_snapshot_marker(self) -> str:
		"""Present iff the snapshot pair below is COMPLETE and matches the disk —
		written last on stop, consumed (removed) before resume on restore. The
		launcher keys off it: marker present → start Firecracker idle (no
		--config-file) for vm-restore.py to load into; absent → normal cold boot."""
		return f"{self.memory_snapshot_directory}/READY"

	@property
	def memory_snapshot_vmstate(self) -> str:
		return f"{self.memory_snapshot_directory}/vmstate.bin"

	@property
	def memory_snapshot_mem(self) -> str:
		return f"{self.memory_snapshot_directory}/mem.bin"

	@property
	def memory_snapshot_signature(self) -> str:
		"""The host signature captured with a warm golden snapshot, staged beside
		the marker by provision-vm.py. vm-restore.py compares it to the live host
		before loading: a snapshot is only restorable on the CPU/kernel/Firecracker
		it was captured on, and a mismatch must cold-boot instead. Absent for the
		same-VM fast stop/start pair (same host by construction)."""
		return f"{self.memory_snapshot_directory}/host-signature.json"

	@property
	def metadata_file(self) -> str:
		"""The MMDS payload staged for a warm clone: this VM's identity (addresses,
		hostname, machine-id, SSH key), served to the guest at 169.254.169.254 so
		the in-guest freshen unit can adopt it after a restore — the clone's disk
		is never mutated offline (the frozen RAM's filesystem cache must keep
		matching it), so MMDS is the only identity channel. Absent for every
		ordinary VM."""
		return f"{self.jail_root}/metadata.json"

	# Jail-relative forms for the Firecracker API bodies — the jailed process
	# resolves snapshot paths after chroot, so they are relative to jail_root
	# (same convention as firecracker.json's rootfs.ext4 / vmlinux).
	memory_snapshot_vmstate_in_jail: str = "snapshot/vmstate.bin"
	memory_snapshot_mem_in_jail: str = "snapshot/mem.bin"
	metadata_in_jail: str = "metadata.json"

	@property
	def api_socket_directory(self) -> str:
		"""Directory holding firecracker.socket. Callers `cd` here (as root —
		it is 0700-owned by the per-VM uid) and address the socket by its short
		relative name, dodging the sun_path limit."""
		return f"{self.jail_root}/run"

	@property
	def api_socket(self) -> str:
		"""Absolute socket path — for stat()/existence checks only (stat has no
		length limit). NEVER pass this to curl --unix-socket; use the relative
		name from api_socket_directory."""
		return f"{self.api_socket_directory}/firecracker.socket"

	@property
	def api_socket_name(self) -> str:
		"""The short relative name to hand curl --unix-socket after cd-ing into
		api_socket_directory."""
		return "firecracker.socket"

	@property
	def systemd_unit(self) -> str:
		"""The per-VM systemd instance name."""
		return f"firecracker-vm@{self.uuid}.service"


def image_directory(image_name: str) -> str:
	return f"{IMAGES_DIRECTORY}/{image_name}"


def warm_snapshot_directory(snapshot_name: str) -> str:
	"""The durable artifact directory of one warm Virtual Machine Snapshot:
	vmstate.bin + mem.bin (the paused-instant pair warm-snapshot-vm.py captured)
	and host-signature.json. provision-vm.py hard-links the pair into each clone
	jail (N clones CoW-share one read-only mem file); the snapshot row's on_trash
	removes the directory."""
	return f"{SNAPSHOTS_DIRECTORY}/{snapshot_name}"
