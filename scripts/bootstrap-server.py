#!/usr/bin/env python3
# Turn a fresh Ubuntu 24.04 host into a Firecracker host.
# Idempotent. Re-run after editing this file to roll forward.
#
# Successor to bootstrap-server.sh. The Task contract is now typed at both ends:
# BootstrapInputs.from_args() parses the CLI flags that used to be env vars; the
# host facts the controller used to scrape off stdout (the trailing
# `cat bootstrap.json`) are now emitted as one machine-readable BootstrapResult
# line. The bytes still land in /var/lib/atlas/bootstrap.json (the canonical
# source of truth), but stdout carries the typed result, not the raw json.

import dataclasses
import json
import os
import sys
import time
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, install_file, run, run_input, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool
from atlas.network_env import default_route_device

# --- the big sysctl drop-in, verbatim from the shell heredoc (step 4) ---
SYSCTL_CONF = """\
# --- VM networking (required; deliberate CIS 3.3.1 deviation, v4 + v6) ---
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.forwarding = 1
net.ipv6.conf.all.proxy_ndp = 1
net.ipv4.ip_forward = 1

# --- CIS 3.3 network hardening (compatible with a routing host) ---
# 3.3.5/3.3.6 a hostile guest must not inject routes via ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
# 3.3.2 we are not a redirect-sending router
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
# 3.3.8 source-routed packets are a spoofing vector
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0
# 3.3.9 log spoofed/source-routed/redirect martians
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1
# 3.3.3 ignore bogus ICMP error responses
net.ipv4.icmp_ignore_bogus_error_responses = 1
# 3.3.4 ignore broadcast ICMP (smurf)
net.ipv4.icmp_echo_ignore_broadcasts = 1
# 3.3.10 SYN cookies blunt SYN floods
net.ipv4.tcp_syncookies = 1
# 3.3.11 guests use static addressing, not SLAAC; host must not accept RAs
net.ipv6.conf.all.accept_ra = 0
net.ipv6.conf.default.accept_ra = 0
# NOTE: rp_filter (CIS 3.3.7) is intentionally omitted — strict reverse-path
# filtering can drop the asymmetric traffic of the routed-tap topology. Add it
# only after confirming on a live bench that egress/ingress paths survive it.
"""

# --- sshd hardening drop-in, verbatim (step 5) ---
SSHD_CONF = """\
# 5.1.20 key-only root (NOT `no`: Atlas operates as root, no unpriv user yet)
PermitRootLogin prohibit-password
# turn "we happen to use keys" into "the server refuses anything else"
PasswordAuthentication no
KbdInteractiveAuthentication no
# 5.1.19 no empty-password accounts may log in
PermitEmptyPasswords no
# 5.1.16 cap auth attempts per connection
MaxAuthTries 4
# 5.1.13 drop slow/abandoned pre-auth connections
LoginGraceTime 60
# 5.1.7 reap dead sessions. Probes are answered by the client's ssh transport
# at the protocol layer (independent of task output), so long silent tasks
# (e.g. apt-get, ~1800s) stay connected; 300x3=900s only reaps a dead client.
ClientAliveInterval 300
ClientAliveCountMax 3
# 5.1.6/5.1.15/5.1.12 modern algorithms only (sets a default OpenSSH client
# negotiates — the e2e SSH connecting at all is the regression guard).
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com,umac-128-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org,diffie-hellman-group16-sha512,diffie-hellman-group18-sha512
"""

# --- kernel-module blocklist, verbatim (step 6) ---
MODPROBE_BLOCKLIST_CONF = """\
# 1.1.1.x unused filesystem modules
install cramfs /bin/false
install freevxfs /bin/false
install hfs /bin/false
install hfsplus /bin/false
install jffs2 /bin/false
install udf /bin/false
install usb-storage /bin/false
blacklist cramfs
blacklist freevxfs
blacklist hfs
blacklist hfsplus
blacklist jffs2
blacklist udf
blacklist usb-storage
# 3.2.x unused network-protocol modules (remote attack surface)
install dccp /bin/false
install tipc /bin/false
install rds /bin/false
install sctp /bin/false
blacklist dccp
blacklist tipc
blacklist rds
blacklist sctp
"""

# --- unattended-upgrades drop-in, verbatim (step 7) ---
UNATTENDED_CONF = """\
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
"""

# Step 2 package set, verbatim.
PACKAGES = [
	"ca-certificates",
	"curl",
	"e2fsprogs",
	"iproute2",
	"jq",
	"lvm2",
	"nftables",
	"squashfs-tools",
	"thin-provisioning-tools",
	"wireguard-tools",
]


@dataclass(frozen=True)
class BootstrapInputs(TaskInputs):
	"""Turn a fresh Ubuntu 24.04 host into a Firecracker host (idempotent)."""

	command: typing.ClassVar[str] = "bootstrap-server"
	firecracker_version: str  # e.g. v1.15.1
	sshpiper_version: str  # e.g. v1.15.1
	architecture: str  # e.g. x86_64 (must match `uname -m`)


@dataclass(frozen=True)
class BootstrapResult(TaskResult):
	firecracker_version: str
	sshpiper_version: str
	jailer_version: str
	kernel_version: str
	architecture: str


def _uname(flag: str) -> str:
	"""`uname -m` / `uname -r` as a stripped string."""
	return run("uname", flag).strip()


def _binary_version(binary: str) -> str:
	"""`<binary> --version | head -n1 | awk '{print $2}'`, '' if absent. The
	`|| true` on the shell side means a missing binary yields an empty string,
	not a failure (the version gate treats absence and wrong-version alike).

	A non-existent executable makes `subprocess.run` raise `FileNotFoundError`
	*before* the exit code is seen, so `check=False` alone does not reproduce the
	shell's `|| true` — the shell turned "command not found" (exit 127) into "".
	Catch it here so absence is the empty string, matching the docstring and the
	version gate's "absent or wrong-version → reinstall" contract."""
	try:
		out = run(binary, "--version", check=False, quiet=True)
	except FileNotFoundError:
		return ""
	first = out.splitlines()[0] if out else ""
	fields = first.split()
	return fields[1] if len(fields) > 1 else ""


def _wait_for_apt_locks() -> None:
	"""Belt-and-suspenders: poll the apt/dpkg locks in case unattended-upgrades or
	a late apt timer still holds them after cloud-init reports done. Cap the wait
	so a genuinely stuck lock still surfaces as a bootstrap failure rather than
	hang."""
	deadline = time.monotonic() + 300
	locks = ["/var/lib/apt/lists/lock", "/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock"]
	while run_ok("sudo", "fuser", *locks):
		if time.monotonic() >= deadline:
			sys.exit("apt/dpkg lock still held after 300s; aborting bootstrap")
		print("waiting for apt/dpkg lock to be released...", file=sys.stderr)
		time.sleep(5)


def _install_firecracker(version: str, architecture: str) -> None:
	"""Install Firecracker + jailer binaries. Both ship in the same release
	tarball; production runs every VM under the jailer (de-privileged, chrooted,
	cgroup-isolated), so we install both. Gate on EITHER binary being absent or
	at the wrong version, so a host bootstrapped before the jailer existed picks
	it up on re-run."""
	installed_firecracker = _binary_version("/usr/local/bin/firecracker")
	installed_jailer = _binary_version("/usr/local/bin/jailer")
	wanted_version = version.lstrip("v")
	if installed_firecracker == wanted_version and installed_jailer == wanted_version:
		return

	release = f"release-{version}-{architecture}"
	url = (
		f"https://github.com/firecracker-microvm/firecracker/releases/download/"
		f"{version}/firecracker-{version}-{architecture}.tgz"
	)
	run("sudo", "rm", "-rf", "/tmp/firecracker-install")
	run("mkdir", "/tmp/firecracker-install")
	# curl … | tar -xz, run from the install dir.
	run("sudo", "sh", "-c", f"cd /tmp/firecracker-install && curl -fsSL {url!r} | tar -xz")
	run(
		"sudo",
		"install",
		"-m",
		"0755",
		f"/tmp/firecracker-install/{release}/firecracker-{version}-{architecture}",
		"/usr/local/bin/firecracker",
	)
	run(
		"sudo",
		"install",
		"-m",
		"0755",
		f"/tmp/firecracker-install/{release}/jailer-{version}-{architecture}",
		"/usr/local/bin/jailer",
	)
	run("sudo", "rm", "-rf", "/tmp/firecracker-install")

def _install_sshpiper(version: str, architecture: str) -> None:
	installed_sshpiper = _binary_version("/usr/local/bin/sshpiperd")
	# TODO: Setup a better way to download plugin
	#installed_plugin = _binary_version("/usr/local/bin/sshpiperd_atlas")
	# TODO: sshpiper's version output is pretty verbose (and sometimes version is in line 2), gotta fix this
	#wanted_version = version.lstrip("v")
	#if installed_firecracker == wanted_version and installed_jailer == wanted_version:
	#	return

	release = f"release-{version}-{architecture}"
	url = (
		f"https://github.com/tg123/sshpiper/releases/download/"
		f"{version}/sshpiperd_with_plugins_linux_{architecture}.tar.gz"
	)
	run("sudo", "rm", "-rf", "/tmp/sshpiper-install")
	run("mkdir", "/tmp/sshpiper-install")
	# curl … | tar -xz, run from the install dir.
	run("sudo", "sh", "-c", f"cd /tmp/sshpiper-install && curl -fsSL {url!r} | tar -xz")
	run(
		"sudo",
		"install",
		"-m",
		"0755",
		f"/tmp/sshpiper-install/sshpiperd",
		"/usr/local/bin/sshpiperd",
	)
	run("sudo", "rm", "-rf", "/tmp/sshpiper-install")

def main() -> None:
	inputs = BootstrapInputs.from_args()

	# Architecture must match the host.
	host_arch = _uname("-m")
	if host_arch != inputs.architecture:
		sys.exit(f"Architecture mismatch: host is {host_arch}, expected {inputs.architecture}")

	# 1. KVM must be present.
	if not os.access("/dev/kvm", os.R_OK) or not os.access("/dev/kvm", os.W_OK):
		sys.exit("/dev/kvm not available. Server must support nested virtualization.")

	# 2. Install packages.
	#    A freshly-booted cloud image still has cloud-init / unattended-upgrades
	#    running its own apt for the first minutes, holding the apt locks. apt's
	#    `DPkg::Lock::Timeout` does NOT cover the `apt-get update` *lists* lock
	#    (/var/lib/apt/lists/lock) on this apt version, so update failed fast with
	#    "Could not get lock" and left fresh droplets Broken. Wait for cloud-init
	#    to finish and the locks to clear before touching apt at all.
	os.environ["DEBIAN_FRONTEND"] = "noninteractive"

	# cloud-init owns the first-boot apt run; block until it's done (best-effort —
	# `status --wait` returns promptly if cloud-init isn't present or already done).
	run("sudo", "cloud-init", "status", "--wait", check=False, quiet=True)

	_wait_for_apt_locks()

	run("sudo", "apt-get", "-o", "DPkg::Lock::Timeout=300", "update")
	run("sudo", "apt-get", "-o", "DPkg::Lock::Timeout=300", "install", "-y", *PACKAGES)

	# 3A Install Firecracker + jailer (version-gated).
	_install_firecracker(inputs.firecracker_version, inputs.architecture)

	# 3B Install SSHPiper + Plugin (version-gated).
	_install_sshpiper(inputs.sshpiper_version, inputs.architecture)
	run("sudo", "systemctl", "enable", "sshpiper.service", check=False, quiet=True)

	# 4. Kernel/network sysctls: VM-networking essentials + CIS 3.3 hardening.
	#    The forwarding + proxy_ndp lines are LOAD-BEARING for the routed-tap VM
	#    networking model (each VM is a per-VM tap, no bridge; the host routes
	#    eth0<->tap, which IS forwarding). IPv6 is the guest's public address;
	#    IPv4 forwarding backs the NAT44 egress masquerade (step 9a). CIS 3.3.1
	#    says disable forwarding — we DO NOT, by design (both families), see
	#    spec/03-bootstrapping.md "Host hardening". Blast radius is contained at
	#    the `inet atlas` nft chains, not here. The remaining lines are CIS 3.3
	#    controls that a routing host still wants.
	install_file(SYSCTL_CONF, "/etc/sysctl.d/60-atlas.conf", mode="0644")
	run("sudo", "sysctl", "--system", quiet=True)

	# 5. sshd hardening (CIS 5.1). A drop-in so we never edit the stock config and
	#    survive package upgrades. Atlas connects key-only as root, so these only
	#    tighten what is already true. PermitRootLogin stays `prohibit-password`
	#    (NOT `no`) — there is no unprivileged user yet and Atlas SSHes as root;
	#    `no` would lock Atlas out of every server (deliberate CIS 5.1.20
	#    deviation, see spec/03-bootstrapping.md "Host hardening").
	install_file(SSHD_CONF, "/etc/ssh/sshd_config.d/60-atlas.conf", mode="0644")
	# Validate BEFORE reload so a bad drop-in can never brick SSH (fail loud).
	run("sudo", "sshd", "-t")
	run("sudo", "systemctl", "reload", "ssh")

	# 6. Kernel-module blocklist (CIS 1.1.1 filesystems + 3.2 network protocols).
	#    `install <m> /bin/false` defeats modprobe; `blacklist` covers autoload.
	#    squashfs is DELIBERATELY ABSENT — unsquashfs unpacks the rootfs image, so
	#    blocklisting it would break image sync (deliberate CIS 1.1.1.7 deviation).
	#    We never blocklist load-bearing modules: tun/tap (VM taps), kvm/kvm_intel/
	#    kvm_amd (Firecracker), vhost/vhost_net (virtio), nf_tables/nft_* (firewall).
	install_file(MODPROBE_BLOCKLIST_CONF, "/etc/modprobe.d/60-atlas-blocklist.conf", mode="0644")

	# 7. Automatic security updates (CIS 1.2.2.1). Security pocket ONLY — we do not
	#    want a feature kernel rolling under a running Firecracker host. No auto
	#    reboot: a security kernel needs a reboot to take effect, but an unattended
	#    reboot would kill every running VM; the operator reboots on a window.
	run("sudo", "apt-get", "-o", "DPkg::Lock::Timeout=300", "install", "-y", "unattended-upgrades")
	install_file(UNATTENDED_CONF, "/etc/apt/apt.conf.d/60-atlas-unattended.conf", mode="0644")

	# 8. Firecracker host controls (prod-host-setup.md): no cross-VM memory side
	#    channel, no guest RAM on disk. Both idempotent; guarded for absence.
	#    KSM (page dedup across VMs) is a side channel — turn it off if present.
	#    `/sys/kernel/mm/ksm/run` is a sysfs control node: it can be WRITTEN but
	#    not unlinked/recreated, so `install` (which unlink+creates) fails with
	#    "cannot remove … Operation not permitted". The shell wrote it in place
	#    (`echo 0 > …`); the in-place Python equivalent is `tee` (truncating open),
	#    not install_file. Same reason the LVM nodes use mknod, not install.
	if os.access("/sys/kernel/mm/ksm/run", os.W_OK):
		run_input("sudo", "tee", "/sys/kernel/mm/ksm/run", stdin="0\n")
	# Swap lets guest memory hit disk (data remanence). DO droplets are typically
	# swapless; swapoff -a is idempotent and a no-op when there is no swap.
	run("sudo", "swapoff", "-a")

	# 9. nftables scaffold. Two-shot: create-if-missing, then ensure chains exist.
	#    One inet table holds both the v6 forward chain and the v4 egress NAT.
	if not run_ok("sudo", "nft", "list", "table", "inet", "atlas"):
		run("sudo", "nft", "add", "table", "inet", "atlas")
	if not run_ok("sudo", "nft", "list", "chain", "inet", "atlas", "forward"):
		run(
			"sudo",
			"nft",
			"add chain inet atlas forward { type filter hook forward priority filter; policy accept; }",
		)

	# 9a. IPv4 egress: masquerade the per-VM private /30s (carved from
	#     100.64.0.0/16) out the host's public uplink. One host-wide rule covers
	#     every VM — the source range is fixed, so no per-VM NAT churn. The guest
	#     is reachable from outside over IPv6 only; this gives it *outbound* v4.
	uplink = default_route_device()
	if not run_ok("sudo", "nft", "list", "chain", "inet", "atlas", "postrouting"):
		run(
			"sudo",
			"nft",
			"add chain inet atlas postrouting { type nat hook postrouting priority srcnat; policy accept; }",
		)
	postrouting = run("sudo", "nft", "list", "chain", "inet", "atlas", "postrouting")
	if "ip saddr 100.64.0.0/16" not in postrouting:
		run(
			"sudo",
			"nft",
			"add",
			"rule",
			"inet",
			"atlas",
			"postrouting",
			"ip",
			"saddr",
			"100.64.0.0/16",
			"oifname",
			uplink,
			"masquerade",
		)

	# 10. Directories.
	install_directory("/var/lib/atlas", mode="0700")
	install_directory("/var/lib/atlas/images", mode="0700")
	install_directory("/var/lib/atlas/virtual-machines", mode="0700")
	install_directory("/var/lib/atlas/run", mode="0700")
	install_directory("/var/lib/atlas/bin", mode="0755")

	# 11. Helper scripts and systemd unit are uploaded alongside this script by
	#     the caller, into /var/lib/atlas/bin/ and /etc/systemd/system/. See
	#     spec/03-bootstrapping.md for the exact list. scp preserves source perms,
	#     so set the executable bit here to be safe.
	#
	#     The .py cutover means the durable hooks under /var/lib/atlas/bin are now
	#     vm-network-up.py / vm-network-down.py / vm-disk-up.py (plus the bin/atlas
	#     package) — there are no *.sh there anymore. The systemd units invoke them
	#     as `python3 <path> %i`, so +x is belt-and-suspenders, not required. Use
	#     `find -exec` (not a `*.sh`/`*.py` glob): a glob that matches nothing is
	#     passed literally to chmod and fails the whole bootstrap, which is exactly
	#     how the stale `*.sh` glob bricked a fresh host. `find` no-ops on absence.
	run(
		"sudo",
		"find",
		"/var/lib/atlas/bin",
		"-maxdepth",
		"1",
		"-name",
		"*.py",
		"-exec",
		"chmod",
		"0755",
		"{}",
		"+",
	)
	run("sudo", "systemctl", "daemon-reload")

	# 11a. LVM thin pool for VM disks. Per-VM disks are thin CoW snapshots of a
	#      read-only base image LV instead of full file copies. The pool's PV is
	#      chosen by ThinPool's PoolBacking: a bare-metal box with spare NVMe
	#      (Scaleway Elastic Metal) backs it with the real disk(s); a stock DO
	#      droplet — whose only disk is partitioned + mounted as root — has no
	#      unused disk, so it falls back to a sparse loopback file on the root fs.
	#      The operator can force a device with ATLAS_POOL_DEVICE. dm_thin_pool is
	#      the kernel target the pool runs on; load it now and persist it for
	#      reboots. The 60-atlas-blocklist (step 6) targets unused fs/net modules
	#      only and never touches dm_*, so this is unaffected. ThinPool.ensure()
	#      (idempotent) does the pv/vg/pool work; atlas-pool.service re-asserts the
	#      backing (re-binding the loop device for the file backing; a real-device
	#      PV needs nothing) after a reboot since bootstrap is not re-run on boot.
	run("sudo", "modprobe", "dm_thin_pool")
	install_file("dm_thin_pool\n", "/etc/modules-load.d/60-atlas-lvm.conf", mode="0644")
	ThinPool().ensure()
	run("sudo", "systemctl", "enable", "atlas-pool.service", check=False, quiet=True)

	# 12. Record state for Atlas to pick up. Single JSON file is the canonical
	#     source of truth. The bytes still land in /var/lib/atlas/bootstrap.json;
	#     BootstrapResult.emit() carries the same values on stdout as the typed
	#     Task result (replacing the trailing `cat bootstrap.json`).
	result = BootstrapResult(
		firecracker_version=_binary_version("/usr/local/bin/firecracker"),
		jailer_version=_binary_version("/usr/local/bin/jailer"),
		sshpiper_version=_binary_version("/usr/local/bin/sshpiperd"),
		kernel_version=_uname("-r"),
		architecture=_uname("-m"),
	)
	install_directory("/var/lib/atlas", mode="0755")
	install_file(_bootstrap_json(result), "/var/lib/atlas/bootstrap.json", mode="0644")

	result.emit()
	print("Bootstrapped Firecracker host.")


def _bootstrap_json(result: BootstrapResult) -> str:
	"""The canonical /var/lib/atlas/bootstrap.json bytes — the same four keys the
	shell `jq -nc` wrote, kept as the on-disk source of truth."""
	return json.dumps(dataclasses.asdict(result))


if __name__ == "__main__":
	main()
