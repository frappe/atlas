"""Networking helpers: IPv6 carve, MAC/tap derivation, IPv6 allocation, IPv4 egress link.

Also holds the jailer-isolation derivations — per-VM uid/gid, network-namespace
and veth-pair names, and the cgroup/rlimit argument strings. Like `derive_mac`
and `derive_tap`, these are pure functions of the VM's UUID (and, for the caps,
its own resource fields), so the on-host jail is fully reconstructible from the
Frappe row with no allocator and no extra DocType state.
"""

import ipaddress
import uuid

import frappe

# Per-VM POSIX uid/gid the jailer drops Firecracker to. Derived from the UUID so
# every VM gets a distinct, stable id with no allocator and no /etc/passwd row
# (the jailer takes a numeric --uid/--gid and chowns by number — Linux does not
# require a passwd entry for a uid to own files or run a process). The window
# sits well above system (<1000) and normal-login (1000-60000) ranges.
UID_BASE = 200000
UID_SPAN = 60000

# Headroom over the guest's RAM for the Firecracker process's own VMM/IO/vCPU
# threads and page-cache churn, so `memory.max` bounds the whole process without
# OOM-killing a healthy VM. Too tight surfaces loudly as a failed-to-start unit.
MEMORY_HEADROOM_MIB = 256

# rlimit on open file descriptors for the jailed process. The jailer defaults to
# 2048 when unset; 1024 is ample for one Firecracker (a handful of fds: kvm,
# tap, drives, socket) and bounds a runaway.
MAX_OPEN_FILES = 1024

# Private (RFC 6598 CGNAT) supernet for per-VM NAT44 egress links. Chosen over
# RFC 1918 so it cannot collide with a Self-Managed host's own LAN or with a
# cloud provider's internal addressing. The address is masqueraded at the host
# uplink and is never visible on the wire — it only needs to be unique per host.
IPV4_EGRESS_SUPERNET = "100.64.0.0/16"


def carve_virtual_machine_range(host_address: str, prefix_cidr: str) -> str:
	"""Return the /124 inside `prefix_cidr` that contains `host_address`.

	DigitalOcean assigns a /64 to each droplet but only the /124 around the
	host's own address is routable inside DO's fabric — addresses elsewhere
	in the /64 are dropped at the upstream edge. We hand out addresses
	inside that /124 only.
	"""
	network = ipaddress.IPv6Network(prefix_cidr, strict=False)
	host = ipaddress.IPv6Address(host_address)
	if host not in network:
		raise ValueError(f"{host_address} is not inside {prefix_cidr}")
	return str(ipaddress.IPv6Network(f"{host_address}/124", strict=False))


def derive_mac(virtual_machine_name: str) -> str:
	"""06:00:<first 4 bytes of UUID>, hex-colons.

	Example: '06:00:d4:f7:c1:a2'. The 06:00 prefix is a locally administered,
	unicast OUI per IEEE 802.
	"""
	hex_only = uuid.UUID(virtual_machine_name).hex
	octets = [hex_only[i:i + 2] for i in range(0, 8, 2)]
	return "06:00:" + ":".join(octets)


def derive_tap(virtual_machine_name: str) -> str:
	"""atlas-<first 9 hex chars of UUID>. Length 15, IFNAMSIZ-safe.

	Linux IFNAMSIZ is 16 bytes including the null terminator, so 15 chars
	is the real max usable length. `atlas-` (6) + 9 hex = 15.
	"""
	hex_only = uuid.UUID(virtual_machine_name).hex
	return f"atlas-{hex_only[:9]}"


def allocate_ipv6(server_name: str) -> str:
	"""Lowest unused address in the server's /124.

	Skips ::0 (subnet id) and ::1 (host). A VM in status Terminated has
	released its address back into the pool — only live (non-Terminated)
	VMs count as occupying an address.
	"""
	server = frappe.get_doc("Server", server_name, for_update=True)
	network = ipaddress.IPv6Network(server.ipv6_virtual_machine_range)
	used = {
		str(ipaddress.IPv6Address(address))
		for address in frappe.get_all(
			"Virtual Machine",
			filters={"server": server_name, "status": ["!=", "Terminated"]},
			pluck="ipv6_address",
		)
		if address
	}
	for index, candidate in enumerate(network.hosts()):
		# IPv6Network.hosts() already excludes ::0 (subnet anycast); we additionally
		# skip ::1, which the host (server) uses. Allocation starts at ::2.
		if index < 1:
			continue
		if str(candidate) not in used:
			return str(candidate)
	raise frappe.ValidationError("No IPv6 capacity on server")


def derive_uid(virtual_machine_name: str) -> int:
	"""Per-VM POSIX uid the jailer runs Firecracker as.

	`UID_BASE + (first 3 bytes of the UUID) % UID_SPAN`, e.g. 247312. Stable
	across reboots and re-provisions (pure function of the UUID), distinct per VM
	so a breakout of one jail cannot touch another VM's files. gid == uid (a
	matching per-VM group). Provision fails loud if a *different* live VM on the
	same host already owns the derived uid (a mod collision), rather than silently
	sharing it.
	"""
	first_three_bytes = int(uuid.UUID(virtual_machine_name).hex[:6], 16)
	return UID_BASE + first_three_bytes % UID_SPAN


def derive_netns(virtual_machine_name: str) -> str:
	"""Per-VM network namespace name: `atlas-<first 12 hex of UUID>`.

	Network-namespace names have no IFNAMSIZ limit, so we use 12 hex chars for
	legibility (the tap inside it keeps the 15-char IFNAMSIZ-safe `derive_tap`
	name). The jailer joins this namespace via `--netns /var/run/netns/<name>`.
	"""
	hex_only = uuid.UUID(virtual_machine_name).hex
	return f"atlas-{hex_only[:12]}"


def derive_veth_pair(virtual_machine_name: str) -> tuple[str, str]:
	"""(host_side, namespace_side) veth interface names.

	`atlas-h<7 hex>` lives in the host netns and carries the VM's /128 onward to
	the uplink; `atlas-n<7 hex>` is moved into the VM's namespace as its default
	route out. Both are 15 chars (`atlas-` + 1 + 7 + the h/n tag — 6+1+1+7=15),
	IFNAMSIZ-safe like `derive_tap`, and distinct from the tap name.
	"""
	hex_only = uuid.UUID(virtual_machine_name).hex
	short = hex_only[:7]
	return f"atlas-h{short}", f"atlas-n{short}"


def cgroup_args(vcpus: int, memory_megabytes: int, disk_gigabytes: int) -> list[str]:
	"""Jailer `--cgroup` flags bounding the VM's memory and CPU (cgroup v2).

	- `memory.max` = guest RAM + headroom (whole-process ceiling).
	- `memory.swap.max` = 0 — never swap guest RAM to host disk (also the
	  per-VM form of Firecracker's "disable swap / data-remanence" guidance).
	- `cpu.max` = `<vcpus * period> <period>` — `vcpus` cores' worth of CPU
	  bandwidth per 100 ms period (bandwidth cap, not cpuset pinning).

	`disk_gigabytes` is unused here (disk is bounded via the `fsize` rlimit, see
	`resource_limit_args`) but kept in the signature so the one call site passes
	the VM's full resource triple.
	"""
	_ = disk_gigabytes
	period_us = 100000
	memory_max_bytes = (memory_megabytes + MEMORY_HEADROOM_MIB) * 1024 * 1024
	cpu_quota_us = vcpus * period_us
	return [
		"--cgroup",
		f"memory.max={memory_max_bytes}",
		"--cgroup",
		"memory.swap.max=0",
		"--cgroup",
		f"cpu.max={cpu_quota_us} {period_us}",
	]


def resource_limit_args(disk_gigabytes: int) -> list[str]:
	"""Jailer `--resource-limit` flags (setrlimit) bounding fds and file size.

	`fsize` caps any single file the jailed process can create at the VM's disk
	size plus 1 GiB of slack (the rootfs is already that large; the slack covers
	the API socket, logs, and Firecracker's own scratch without letting a runaway
	fill the host). `no-file` bounds the descriptor count.
	"""
	fsize_bytes = (disk_gigabytes + 1) * 1024 * 1024 * 1024
	return [
		"--resource-limit",
		f"fsize={fsize_bytes}",
		"--resource-limit",
		f"no-file={MAX_OPEN_FILES}",
	]


def derive_ipv4_link(ipv6_address: str) -> tuple[str, str]:
	"""(host_side, guest_side) /30 CIDRs for a VM's private NAT44 egress link.

	The guest's private IPv4 is masqueraded at the host uplink and never seen
	on the wire, so it only needs to be unique per host. We derive it from the
	VM's already-allocated IPv6 address — no separate allocator and no DocType
	field — exactly like `derive_mac` / `derive_tap`.

	Each VM gets a point-to-point /30 inside `IPV4_EGRESS_SUPERNET`, indexed by
	the low bits of its IPv6 address. A /124 v6 range yields indices 2..15
	(::0/::1 are never handed to VMs); a larger Self-Managed range stays unique
	as long as it fits the /16 (16384 /30 links). Mirrors the v6 host part so
	one VM's v4 and v6 share an index — easy to correlate in `ip addr`.

	Example: ::2 -> ('100.64.0.9/30', '100.64.0.10/30').
	"""
	supernet = ipaddress.IPv4Network(IPV4_EGRESS_SUPERNET)
	index = int(ipaddress.IPv6Address(ipv6_address)) & 0x3FFF
	base = int(supernet.network_address) + index * 4
	link = ipaddress.IPv4Network((base, 30))
	if not supernet.supernet_of(link):
		raise frappe.ValidationError("No IPv4 egress capacity on server")
	hosts = list(link.hosts())
	return (
		f"{hosts[0]}/{link.prefixlen}",
		f"{hosts[1]}/{link.prefixlen}",
	)
