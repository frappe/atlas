#!/usr/bin/env python3
# Download a kernel + rootfs pair into /var/lib/atlas/images/<image_name>/.
# Convert the squashfs rootfs into a pristine ext4 of <default_disk_gb>.
# Install the in-guest network unit so VMs come up with static IPv6.
# Idempotent: if files exist with matching checksums, exit early.
#
# Successor to sync-image.sh. Inputs that the shell read as `${VAR}` env vars are
# now typed CLI flags via SyncImageInputs.from_args(); the body never touches
# os.environ. No machine-readable result line — the controller parses nothing
# back, so we just print a human "Image <name> ready." like the original.

import os
import sys
import typing
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import _substitute, install_directory, install_file, run, run_input, shell
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import image_directory

# Where the controller stages the guest atlas-network.service sidecar before a
# sync-image Task (SCRIPT_SIDECARS in script_uploads.py). This is ALWAYS the path
# for a controller-driven sync, so SyncImageInputs.guest_network_unit defaults to
# it — an operator hand-running the verb needn't pass --guest-network-unit (they
# either point it at a real file or rely on this staged path being populated).
STAGED_GUEST_NETWORK_UNIT = "/tmp/atlas/atlas-network.service"


@dataclass(frozen=True)
class SyncImageInputs(TaskInputs):
	"""Download + normalize a kernel/rootfs pair into an Atlas base image.

	Every required flag is flat image data (urls, filenames, sha256, disk-gb) an
	operator reads off the image's spec — there are no controller-only required
	flags, so this verb is operator-typable for break-glass. The verb stages the
	guest `atlas-network.service` as a sidecar; --guest-network-unit defaults to that
	staged path and only needs overriding for a hand run pointing at a real file."""

	command: typing.ClassVar[str] = "sync-image"
	image_name: str  # directory name under /var/lib/atlas/images
	kernel_url: str  # HTTPS URL of the packed, zstd-compressed vmlinuz
	kernel_filename: str  # destination filename, e.g. vmlinux-6.1.141
	kernel_sha256: str  # hex digest of the *packed* kernel artifact
	rootfs_url: str  # HTTPS URL of the source squashfs
	rootfs_filename: str  # destination ext4 filename, e.g. ubuntu-24.04.ext4
	rootfs_sha256: str  # hex digest of the *source squashfs*, not the ext4
	default_disk_gb: int  # size of the pristine ext4
	# Server path to the guest atlas-network.service. Defaults to the controller's
	# staged sidecar path so a hand run can omit it; pass a real file to override.
	guest_network_unit: str = field(
		default=STAGED_GUEST_NETWORK_UNIT,
		metadata={
			"help": (
				"server path to the guest atlas-network.service to bake into the image; "
				f"defaults to the controller's staged sidecar at {STAGED_GUEST_NETWORK_UNIT}"
			)
		},
	)


# Minimal /etc/hosts. Per-VM hostname mapping (the 127.0.1.1 line) is added at
# provision time, not here (see step 3a.4).
_HOSTS = """\
127.0.0.1   localhost
::1         localhost ip6-localhost ip6-loopback
fe00::0     ip6-localnet
ff00::0     ip6-mcastprefix
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
"""

# Atlas sshd drop-in — sorts before 60-cloudimg-settings.conf; first match wins
# per directive, so these take effect (see step 3a.5).
_SSHD_DROP_IN = """\
# Atlas-managed: enforce key-only SSH. Sorts before 60-cloudimg-settings.conf;
# first match wins per directive, so these take effect.
PasswordAuthentication no
PermitRootLogin prohibit-password
"""

# Real /etc/fstab — the shipped one literally says UNCONFIGURED. We mount by the
# LABEL mkfs sets in step 4 (atlas-root), stable across copies (see step 3a.8).
_FSTAB = """\
LABEL=atlas-root  /  ext4  defaults,errors=remount-ro  0  1
"""

# Boot-blocking units to mask. The cloud image boots into cloud-init +
# systemd-networkd-wait-online + snapd seeding, all of which hang indefinitely
# under Atlas (no datasource, static v6 brought up by atlas-network.service, no
# need for snap). See step 3a.1b.
#
# systemd-resolved is here too: the cloud image enables it and symlinks
# /etc/resolv.conf -> ../run/systemd/resolve/stub-resolv.conf, pointing the
# system resolver at the 127.0.0.53 stub. Atlas brings the network up statically
# with raw `ip` commands and never feeds resolved an upstream (no DHCP, no
# netplan, no DNS= drop-in), so the stub has zero name servers: `dig
# @2606:4700:4700::1111` works but getaddrinfo()/apt fail. We mask resolved and
# (in 3a.4b) replace the symlink with a real file atlas-network.service owns.
_MASKED_UNITS = (
	"cloud-init.service",
	"cloud-init-local.service",
	"cloud-config.service",
	"cloud-final.service",
	"systemd-networkd-wait-online.service",
	"snapd.seeded.service",
	"snapd.service",
	"snapd.socket",
	"systemd-resolved.service",
)

# Junk units to mask for boot speed. Unlike _MASKED_UNITS above, none of these
# BLOCK boot — they run in parallel off the SSH critical path. We mask them
# anyway because on a single-tenant guest VM they are pure overhead: they burn
# CPU/IO during the boot storm (slowing the units that DO gate sshd) and inflate
# time-to-ready. Measured on a real Firecracker boot, leaving them enabled cost
# (in parallel) apport ~17s, ModemManager ~9s, plus multipathd/udisks2/polkit and
# the snapd leaf units the core snapd mask above misses. Verified: masking these
# survives unsquash -> pack -> provision -> boot, and again through a golden
# snapshot -> clone -> boot, with MariaDB/Redis (load-bearing for a site VM) left
# enabled. NOTE: this does NOT approach a ~1s boot on its own — the dominant
# serial gates are apparmor.service (~10s) and the virtio dev-vda/tmpfiles chain;
# those are the next levers and are deliberately NOT touched here.
_JUNK_UNITS = (
	"apport.service",
	"apport-autoreport.path",
	"apport-autoreport.timer",
	"apport-forward.socket",
	"ModemManager.service",
	"multipathd.service",
	"multipathd.socket",
	"udisks2.service",
	"snapd.apparmor.service",
	"snapd.autoimport.service",
	"snapd.core-fixup.service",
	"snapd.recovery-chooser-trigger.service",
	"snapd.system-shutdown.service",
	"snapd.snap-repair.timer",
	"lxd-installer.socket",
	"polkit.service",
	# pollinate phones entropy.ubuntu.com at boot and dominates userspace boot
	# (~10s). The guest now has virtio-rng (/dev/hwrng) from the host, so the
	# boot-time entropy it seeds is unnecessary. See provision-vm.py "entropy".
	"pollinate.service",
)


def _download_kernel(inputs: SyncImageInputs, image_dir: str) -> None:
	# 1. Kernel. The Ubuntu cloud image ships a packed, zstd-compressed bzImage
	#    (`vmlinuz`); Firecracker needs an uncompressed ELF `vmlinux`. We download
	#    the packed file, verify it against KERNEL_SHA256 (the digest of the
	#    *packed* artifact, from upstream SHA256SUMS), then decompress the zstd
	#    payload to the final vmlinux. The extracted kernel is a derived artifact,
	#    not separately checksummed — verifying the download is the integrity gate.
	kernel_path = f"{image_dir}/{inputs.kernel_filename}"
	if os.path.isfile(kernel_path):
		print("Kernel already present. Skipping.")
		return

	packed_path = f"{kernel_path}.vmlinuz"
	run("sudo rm -f {} {}", f"{packed_path}.part", packed_path)
	run("sudo curl -fsSL --output {} {}", f"{packed_path}.part", inputs.kernel_url)
	run_input("sudo sha256sum -c -", stdin=f"{inputs.kernel_sha256}  {packed_path}.part")
	run("sudo mv {} {}", f"{packed_path}.part", packed_path)

	# Decompress the embedded vmlinux. The Ubuntu kernel is a PE/EFI bzImage
	# whose payload is a zstd frame followed by a 4-byte size trailer, so plain
	# `unzstd`/`zstd -d` reject it ("unsupported format" — trailing bytes after
	# the frame). `zstd -dc -f` decompresses the valid frame and ignores the
	# trailer. We can't use the kernel.org extract-vmlinux helper: it verifies
	# with `readelf`, absent on a stock Firecracker host (it silently yields a
	# 0-byte file). So: locate the zstd magic (28 b5 2f fd), decompress from
	# there with `-f`, and confirm the ELF magic (7f 45 4c 46). `xxd | grep -bo`
	# gives a hex-nibble offset (byte = /2); `tail -c +N` is 1-indexed (+1).
	hex_offset = shell(
		"xxd -p {} | tr -d '\\n' | grep -bo '28b52ffd' | head -1 | cut -d: -f1",
		packed_path,
	).strip()
	if not hex_offset:
		sys.exit(f"No zstd magic in kernel image {packed_path}")
	byte_offset = int(hex_offset) // 2
	inner = _substitute(
		"tail -c +{} {} | zstd -dc -f > {}", (byte_offset + 1, packed_path, f"{kernel_path}.part")
	)
	run("sudo sh -c {}", inner)
	if shell("head -c 4 {} | xxd -p", f"{kernel_path}.part").strip() != "7f454c46":
		run("sudo rm -f {}", f"{kernel_path}.part")
		sys.exit("Decompressed kernel is not ELF")
	run("sudo mv {} {}", f"{kernel_path}.part", kernel_path)
	run("sudo rm -f {}", packed_path)


def _download_rootfs(inputs: SyncImageInputs, image_dir: str) -> str:
	# 2. Rootfs. Returns the extracted directory path; caller normalizes + builds.
	squashfs_path = f"/tmp/atlas-{inputs.image_name}.squashfs"
	extracted_directory = f"/tmp/atlas-{inputs.image_name}-rootfs"
	run("sudo rm -f {} {}", f"{squashfs_path}.part", squashfs_path)
	run("sudo rm -rf {}", extracted_directory)

	run("sudo curl -fsSL --output {} {}", f"{squashfs_path}.part", inputs.rootfs_url)
	run_input("sudo sha256sum -c -", stdin=f"{inputs.rootfs_sha256}  {squashfs_path}.part")
	run("sudo mv {} {}", f"{squashfs_path}.part", squashfs_path)

	run("sudo unsquashfs -d {} {}", extracted_directory, squashfs_path)
	return extracted_directory


def _install_guest_network_unit(inputs: SyncImageInputs, root: str) -> None:
	# 3. Install the guest network unit and a placeholder env file.
	install_directory(f"{root}/etc/systemd/system", mode="0755")
	install_directory(f"{root}/etc/systemd/system/multi-user.target.wants", mode="0755")
	run(
		"sudo install -m 0644 {} {}",
		inputs.guest_network_unit,
		f"{root}/etc/systemd/system/atlas-network.service",
	)
	run(
		"sudo ln -sf /etc/systemd/system/atlas-network.service {}",
		f"{root}/etc/systemd/system/multi-user.target.wants/atlas-network.service",
	)
	install_file("", f"{root}/etc/atlas-network.env", mode="0644")


def _normalize_rootfs(root: str) -> None:
	# 3a. Normalize the rootfs. The Ubuntu cloud image is built for a generic
	#     cloud, not for Atlas's static-IPv6 / no-first-boot-agent model. Left
	#     untouched it (a) blocks boot forever on cloud-init's datasource probe,
	#     systemd-networkd-wait-online, and snapd seeding; (b) ships identical
	#     host keys / machine-id across VMs; (c) trusts cloud-init for identity
	#     Atlas instead injects at mount time. Strip/neutralize all of that once
	#     here so every VM boots straight to a clean login.
	#
	#     NOTE (regression checklist): each step below must be a no-op OR a
	#     correct strip on the current upstream image. If a step's target is
	#     absent on a future image, that step becomes a documented no-op (the
	#     `rm -f` / mask calls stay harmless); it is not silently dropped.

	# 3a.1 Kill fcnet. This is a Firecracker-CI artifact (phantom IPv4/30 from the
	#      MAC); the Ubuntu cloud image has none of these files, so every line is a
	#      no-op today. Kept because `rm -f` is harmless and documents the contract.
	run("sudo rm -f {}", f"{root}/usr/local/bin/fcnet-setup.sh")
	run("sudo rm -f {}", f"{root}/etc/systemd/system/fcnet.service")
	run("sudo rm -f {}", f"{root}/etc/systemd/system/sshd.service.wants/fcnet.service")
	run("sudo rm -f {}", f"{root}/etc/systemd/system/multi-user.target.wants/fcnet.service")

	# 3a.1b Neutralize cloud-init and the boot-blocking services. The cloud image
	#       boots into cloud-init + systemd-networkd-wait-online + snapd seeding,
	#       all of which hang indefinitely under Atlas (no datasource, static v6
	#       brought up by atlas-network.service, no need for snap). Without this the
	#       guest never reaches a login prompt (the e2e guest-identity probe is the
	#       regression guard). We mask the units (symlink to /dev/null) so they
	#       cannot start, and set cloud-init's own disable flag for good measure.
	#       Masking is idempotent and survives even if the package is reinstalled.
	install_directory(f"{root}/etc/cloud", mode="0755")
	run("sudo touch {}", f"{root}/etc/cloud/cloud-init.disabled")
	for unit in _MASKED_UNITS:
		run("sudo ln -sf /dev/null {}", f"{root}/etc/systemd/system/{unit}")

	# 3a.1c Mask the boot-speed junk units (see _JUNK_UNITS). Same /dev/null symlink
	#       mechanism as the boot-blockers above; the difference is intent — these
	#       don't hang boot, they just burn the boot storm. MariaDB/Redis are
	#       deliberately NOT in the list: a site VM needs them.
	for unit in _JUNK_UNITS:
		run("sudo ln -sf /dev/null {}", f"{root}/etc/systemd/system/{unit}")

	# 3a.2 Strip the shipped SSH host keys so every VM doesn't share one
	#      identity. We do NOT rely on first-boot regeneration (cloud-init is
	#      masked, and we don't trust ssh.service keygen); provision-vm.sh writes
	#      fresh per-VM host keys into the mounted rootfs at provision time.
	#      The shell glob (ssh_host_*_key{,.pub}) is expanded by the shell here so
	#      the rm only deletes what actually exists (nullglob-safe via `sh -c`).
	ssh_host_key_prefix = f"{root}/etc/ssh/ssh_host_"
	inner = _substitute("rm -f {}*_key {}*_key.pub", (ssh_host_key_prefix, ssh_host_key_prefix))
	run("sudo sh -c {}", inner)

	# 3a.3 Force regeneration of machine-id on first boot. systemd
	#      repopulates an empty /etc/machine-id at boot if it is zero
	#      bytes (NOT if it is absent — absent triggers a different code
	#      path that breaks journald).
	run("sudo truncate -s 0 {}", f"{root}/etc/machine-id")
	run("sudo rm -f {}", f"{root}/var/lib/dbus/machine-id")

	# 3a.4 Normalize /etc/hosts to a minimal template. Per-VM hostname mapping
	#      (the 127.0.1.1 line) is added at provision time, not here. Overwriting
	#      is correct regardless of what the upstream file contains — Atlas owns it.
	install_file(_HOSTS, f"{root}/etc/hosts", mode="0644")

	# 3a.4b Make /etc/resolv.conf a real, Atlas-owned file. The cloud image ships
	#       it as a symlink to systemd-resolved's stub (../run/systemd/resolve/
	#       stub-resolv.conf). resolved is masked in 3a.1b, but the dangling
	#       symlink would still defeat atlas-network.service's `> /etc/resolv.conf`
	#       (the write would follow the link into a tmpfs path that doesn't exist
	#       at build time and is owned by a dead daemon at runtime). Replace it
	#       with a regular file carrying the Cloudflare v6 resolver; the guest unit
	#       re-asserts the same line at every boot. `rm -f` first so install_file
	#       writes a real file rather than following the symlink.
	run("sudo rm -f {}", f"{root}/etc/resolv.conf")
	install_file("nameserver 2606:4700:4700::1111\n", f"{root}/etc/resolv.conf", mode="0644")

	# 3a.5 Lock root password (key-only by contract) and enforce key-only SSH.
	#      The Ubuntu cloud image's sshd_config has `Include
	#      /etc/ssh/sshd_config.d/*.conf` near the top and ships
	#      `60-cloudimg-settings.conf` enabling PasswordAuthentication. A prepend
	#      to sshd_config would be overridden by that Include, so we drop our own
	#      `00-atlas.conf` into the same directory — it sorts first, and first
	#      match wins per directive, so it beats 60-cloudimg-settings.conf.
	run("sudo sed -i s|^root:[^:]*:|root:!:| {}", f"{root}/etc/shadow")
	install_directory(f"{root}/etc/ssh/sshd_config.d", mode="0755")
	install_file(_SSHD_DROP_IN, f"{root}/etc/ssh/sshd_config.d/00-atlas.conf", mode="0644")

	# 3a.6 Ensure /home/ubuntu is owned by uid/gid 1000 *if it exists*. The
	#      Ubuntu cloud image does NOT ship /home/ubuntu — cloud-init creates the
	#      `ubuntu` user on first boot, and we've masked cloud-init. Atlas SSHes
	#      in as root (key injected at provision time), so the ubuntu user is
	#      irrelevant to us; this is a guarded no-op on the cloud image, kept so a
	#      future image that does ship the dir gets correct ownership.
	if os.path.isdir(f"{root}/home/ubuntu"):
		run("sudo chown -R 1000:1000 {}", f"{root}/home/ubuntu")

	# 3a.7 Quieten the motd. 60-unminimize prints a "this image is
	#      minimized" nag on every login; 50-motd-news fetches news
	#      from Canonical which on v6-only with strict resolv.conf
	#      hangs briefly.
	run(
		"sudo rm -f {} {}",
		f"{root}/etc/update-motd.d/50-motd-news",
		f"{root}/etc/update-motd.d/60-unminimize",
	)

	# 3a.8 Write a real /etc/fstab. The shipped one literally says
	#      UNCONFIGURED. The rootfs UUID is unknown until mkfs runs
	#      (step 4) and stable across copies, so we use the LABEL
	#      mkfs sets in step 4 — see below.
	install_file(_FSTAB, f"{root}/etc/fstab", mode="0644")

	# 3a.9 Drop the PackageKit apt hook. `20packagekit` installs a dpkg
	#      Post-Invoke that `gdbus call`s org.freedesktop.PackageKit over the
	#      system bus after EVERY install, to tell PackageKit the cache changed.
	#      The guest has dbus but PackageKit is masked/absent, so gdbus tries to
	#      D-Bus-activate it, blocks, and hits its own `--timeout 4`, printing a
	#      bare `Error: Timeout was reached` and adding ~7s to every apt run (the
	#      real cause of the "Timeout was reached" reports; NOT mandb or
	#      needrestart). Nothing on a single-tenant guest consumes PackageKit —
	#      removing the hook takes an `apt install` from ~8s to ~1s. `rm -f` is a
	#      documented no-op if a future image drops the file.
	run("sudo rm -f {}", f"{root}/etc/apt/apt.conf.d/20packagekit")


def _manifest_url(rootfs_url: str) -> str:
	"""The cloud image's package manifest sits beside the squashfs: same URL with
	`.squashfs` swapped for `.manifest`. It pins the exact `linux-modules-<kver>`
	version, which is how _install_virtio_rng derives the running kernel version
	instead of hard-coding it (a point-release bump moves both the kernel we boot
	and the module we must match, in lockstep)."""
	if rootfs_url.endswith(".squashfs"):
		return rootfs_url[: -len(".squashfs")] + ".manifest"
	# A non-squashfs rootfs (a future image flavor) has no sibling manifest; the
	# caller treats an empty return as "skip virtio_rng, no kver to match".
	return ""


# The modular kernel drivers we bake into the guest rootfs. The extracted vmlinux
# boots WITHOUT them (Ubuntu ships them as CONFIG=m modules, not built-ins) and the
# cloud squashfs carries an EMPTY /lib/modules (the matching linux-modules package
# was never installed into it), so each would otherwise be missing:
#   * virtio_rng — CONFIG_HW_RANDOM_VIRTIO=m. /dev/hwrng exists but never binds, so
#     the entropy device provision-vm.py attaches can't feed the guest CSPRNG.
#   * zfs (+ its sole dep spl) — CONFIG_ZFS=m. `modprobe zfs` is FATAL, which aborts
#     bench-cli's ZFS volume step at boot ([volume].enabled). Ubuntu ships a PREBUILT
#     zfs.ko in linux-modules-<kver> matched to this exact kernel, so we bake that
#     instead of DKMS-compiling it every bake (DKMS pulls a toolchain + headers and
#     burns minutes of CPU). The kernel is byte-pinned by the dated cloud release, so
#     its vermagic never drifts out from under the prebuilt module.
# All three files live in the SAME linux-modules-<kver> deb, so one download seeds
# both drivers. We locate each by NAME/SUBTREE, not a hard-coded path, so a moved
# subdir or changed compression suffix (.ko.zst vs .ko.xz) still resolves.
_VIRTIO_RNG_MODULE = "virtio_rng"  # modprobe name (underscore); file is virtio-rng.ko*
# zfs.ko depends only on spl.ko (icp/zavl/zcommon/… are compiled into the monolithic
# Ubuntu zfs.ko); both sit under kernel/zfs/, so we copy that whole subtree and let
# depmod resolve the zfs->spl edge from the modules' own `depends=` modinfo.
_ZFS_MODULE = "zfs"
_ZFS_SUBTREE = "kernel/zfs"  # holds zfs.ko* and spl.ko* in linux-modules-<kver>
# depmod reads these alongside the .ko to emit a warning-free modules.dep; they
# ship in the same deb. modules.dep itself is generated by depmod, not copied.
_MODULE_METADATA = ("modules.builtin", "modules.builtin.modinfo", "modules.order")


def _install_guest_modules(root: str, rootfs_url: str) -> None:
	"""Bake the modular drivers the Firecracker guest needs (virtio_rng, zfs) into
	the rootfs, since it boots an external vmlinux against an empty /lib/modules.

	We fetch the manifest-pinned linux-modules-<kver> package ONCE, `dpkg-deb -x` it
	to a scratch dir (the package owns its own paths — immune to layout changes), copy
	ONLY the modules we need + the depmod metadata into the rootfs, run depmod to
	generate modules.dep, and pin each in modules-load.d so systemd-modules-load
	`modprobe`s it at boot. Copying a handful of .ko files rather than `dpkg -i`-ing
	the whole ~32 MB module set keeps every guest ext4 (and its CoW snapshots) lean."""
	manifest_url = _manifest_url(rootfs_url)
	if not manifest_url:
		print("No sibling manifest for this rootfs; skipping guest module bake.")
		return

	# 1. Derive the kernel version from the manifest's linux-modules-<kver> line.
	#    `linux-modules-6.8.0-117-generic\t6.8.0-117.117` -> kver 6.8.0-117-generic.
	manifest = run("curl -fsSL {}", manifest_url)
	kver = ""
	for line in manifest.splitlines():
		package = line.split("\t", 1)[0].strip()
		if package.startswith("linux-modules-") and package.endswith("-generic"):
			kver = package[len("linux-modules-") :]
			break
	if not kver:
		sys.exit(f"No linux-modules-*-generic entry in manifest {manifest_url}")
	print(f"Baking guest modules (virtio_rng, zfs) for kernel {kver}")

	# 2. Download the linux-modules deb into a scratch dir and unpack it with
	#    dpkg-deb -x (lays down its own lib/modules/<kver> tree; we copy out of it,
	#    never installing the full set). apt-get download resolves the exact archive
	#    URL/version from the host's apt metadata.
	work = f"/tmp/atlas-modules-{kver}"
	deb_root = f"{work}/deb"
	run("sudo rm -rf {}", work)
	install_directory(work, mode="0755")
	# apt-get download writes to CWD; run it there and capture the resulting .deb.
	shell("cd {} && apt-get download {}", work, f"linux-modules-{kver}")
	deb = shell("ls {}/linux-modules-{}_*.deb", work, kver).strip().splitlines()[0]
	run("sudo dpkg-deb -x {} {}", deb, deb_root)

	source_modules = f"{deb_root}/lib/modules/{kver}"
	dest_modules = f"{root}/lib/modules/{kver}"

	# 3. Copy virtio_rng by NAME (find, not a hard-coded path) into the SAME relative
	#    location under the rootfs so depmod records the path it actually lives at.
	#    The basename is virtio-rng.ko, virtio-rng.ko.zst, virtio-rng.ko.xz, …
	module_source = shell(
		"find {} -name {} | head -1", source_modules, f"{_VIRTIO_RNG_MODULE.replace('_', '-')}.ko*"
	).strip()
	if not module_source:
		sys.exit(f"{_VIRTIO_RNG_MODULE} not found in linux-modules-{kver}")
	relative = module_source[len(f"{source_modules}/") :]  # kernel/drivers/.../virtio-rng.ko.zst
	module_dest = f"{dest_modules}/{relative}"
	install_directory(os.path.dirname(module_dest), mode="0755")
	run("sudo cp {} {}", module_source, module_dest)

	# 4. Copy the whole kernel/zfs/ subtree (zfs.ko* + spl.ko*) verbatim so depmod
	#    resolves the zfs->spl dependency edge. cp -a preserves the subdir layout the
	#    module paths in modules.dep will reference.
	zfs_source = f"{source_modules}/{_ZFS_SUBTREE}"
	if not shell("find {} -name {} | head -1", zfs_source, f"{_ZFS_MODULE}.ko*").strip():
		sys.exit(f"{_ZFS_MODULE} not found under {_ZFS_SUBTREE} in linux-modules-{kver}")
	install_directory(f"{dest_modules}/{os.path.dirname(_ZFS_SUBTREE)}", mode="0755")
	run("sudo cp -a {} {}", zfs_source, f"{dest_modules}/{_ZFS_SUBTREE}")

	# 5. Copy the depmod metadata that ships in the deb (modules.dep itself is
	#    generated by depmod below, not copied).
	for metadata in _MODULE_METADATA:
		run("sudo cp {} {}", f"{source_modules}/{metadata}", f"{dest_modules}/{metadata}")

	# 6. Generate modules.dep for the rootfs tree so `modprobe <name>` resolves.
	#    -b <root> makes depmod treat the rootfs as / and read the metadata above.
	run("sudo depmod -b {} {}", root, kver)

	# 7. Pin each module so systemd-modules-load.service loads it every boot. Neither
	#    has a udev autoload: virtio_rng backs a built-in-registered virtio-mmio device
	#    with a modular driver, and zfs is loaded on demand by bench-cli's volume step.
	install_directory(f"{root}/etc/modules-load.d", mode="0755")
	pinned = f"{_VIRTIO_RNG_MODULE}\n{_ZFS_MODULE}\n"
	install_file(pinned, f"{root}/etc/modules-load.d/atlas-guest-modules.conf", mode="0644")

	run("sudo rm -rf {}", work)


def _build_ext4(root: str, rootfs_path: str, disk_gb: int) -> None:
	# 4. Build the ext4. Label `atlas-root` matches /etc/fstab.
	#
	# metadata_csum_seed decouples the per-block checksum seed from the
	# filesystem UUID. Without it, `tune2fs -U random` (run per-VM in
	# prepare_lv to give each clone a distinct UUID) must rewrite every
	# metadata block's checksum; on a CoW thin snapshot each such write forces
	# a pool copy, costing ~1.7s per provision. With the seed baked into the
	# base image, the UUID change is a single superblock write (~9ms) on every
	# snapshot. Measured 185x: 1.673s -> 0.009s.
	run("sudo chown -R root:root {}", root)

	# The blanket root:root above clobbers the ownership a handful of paths need.
	# /var/cache/man must be man:man: Ubuntu installs `mandb` SETUID `man`, so the
	# dpkg man-db trigger runs as the `man` user, and systemd-tmpfiles ships
	# `d /var/cache/man 0755 man man` to make the cache man-owned for exactly that
	# reason. tmpfiles never runs at build time, so without this every guest `apt`
	# floods `mandb: can't chmod /var/cache/man/<locale>/CACHEDIR.TAG: Operation
	# not permitted` (harmless, but noisy enough to push apt past the Task timeout).
	# Use the NUMERIC id (man = uid 6, gid 12 on Ubuntu): this chown runs on the
	# host against a foreign rootfs, so a by-name `man:man` would resolve against
	# the HOST's /etc/passwd, not the guest's — correct only while both are Ubuntu.
	# Numeric is host-independent and matches the guest's own passwd. Verified on
	# ubuntu-24.04: 52 errors -> 0.
	run("sudo chown -R 6:12 {}", f"{root}/var/cache/man")

	run("sudo truncate -s {} {}", f"{disk_gb}G", f"{rootfs_path}.part")
	run(
		"sudo mkfs.ext4 -q -O metadata_csum_seed -L atlas-root -d {} -F {}",
		root,
		f"{rootfs_path}.part",
	)
	run("sudo mv {} {}", f"{rootfs_path}.part", rootfs_path)


def main() -> None:
	inputs = SyncImageInputs.from_args()
	pool = ThinPool()

	image_dir = image_directory(inputs.image_name)
	install_directory(image_dir, mode="0700")

	_download_kernel(inputs, image_dir)

	# 2. Rootfs. If the final ext4 is already built, the image is complete.
	rootfs_path = f"{image_dir}/{inputs.rootfs_filename}"
	if os.path.isfile(rootfs_path):
		print("Rootfs already built. Skipping.")
		return

	extracted = _download_rootfs(inputs, image_dir)
	_install_guest_network_unit(inputs, extracted)
	_normalize_rootfs(extracted)
	_install_guest_modules(extracted, inputs.rootfs_url)
	_build_ext4(extracted, rootfs_path, inputs.default_disk_gb)

	squashfs_path = f"/tmp/atlas-{inputs.image_name}.squashfs"
	run("sudo rm -rf {} {}", extracted, squashfs_path)

	# 5. Base image as a read-only thin LV. Per-VM disks are instant CoW snapshots
	#    of this LV instead of full file copies; the pristine ext4 file stays on
	#    disk as the import source and audit artifact. Idempotent — a no-op if the
	#    LV already exists, so a re-sync of an unchanged image touches nothing.
	pool.import_base_image(rootfs_path, inputs.image_name, inputs.default_disk_gb)

	print(f"Image {inputs.image_name} ready.")


if __name__ == "__main__":
	main()
