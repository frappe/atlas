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
