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

# CPU bandwidth models (Virtual Machine.cpu_mode). Both share `cpu_max_cores` as
# the VM's guaranteed share; they differ in whether that share is also a hard
# ceiling. See `cgroup_args`.
CPU_MODE_HARD = "Hard cap"  # cpu.max == cpu_max_cores; the bandwidth cap is a wall.
CPU_MODE_RELAXED = "Relaxed"  # cpu.weight floor + a loose cpu.max burst ceiling.

# In relaxed mode `cpu.weight` carries the guaranteed proportional share. cgroup
# v2 weights live in [1, 10000] (100 = default); we scale `cpu_max_cores` by this
# so a full core is the default weight and a sub-core tier is proportionally
# lighter (1/16 core -> ~6, one core -> 100, two cores -> 200), then clamp into
# range. Capacity accounting stays keyed on `cpu_max_cores`, so the weights sum
# to the same proportions placement already reasons about.
CPU_WEIGHT_PER_CORE = 100
CPU_WEIGHT_MIN = 1
CPU_WEIGHT_MAX = 10000

# rlimit on open file descriptors for the jailed process. The jailer defaults to
# 2048 when unset; 1024 is ample for one Firecracker (a handful of fds: kvm,
# tap, drives, socket) and bounds a runaway.
MAX_OPEN_FILES = 1024

# Private (RFC 6598 CGNAT) supernet for per-VM NAT44 egress links. Chosen over
# RFC 1918 so it cannot collide with a Self-Managed host's own LAN or with a
# cloud provider's internal addressing. The address is masqueraded at the host
# uplink and is never visible on the wire — it only needs to be unique per host.
IPV4_EGRESS_SUPERNET = "100.64.0.0/16"

# Migration tunnel (spec/24-vm-migration.md §2.9, keep-address path). When a VM
# migrates keeping its /128, the source host keeps holding the /64 that /128 is
# carved from, so it keeps receiving the VM's inbound traffic and forwards it to
# the target over a per-VM point-to-point tunnel; the target policy-routes the
# guest's replies back up the same tunnel so egress is always sourced from the
# box that legitimately owns the range (§2.0 — the switch drops any other
# source). The tunnel is a `tun` device whose frames socat bridges to a plain TCP
# stream between the two hosts (unencrypted, matching the stage-1 NBD transport;
# a secure carrier is a deferred follow-up). Everything is a pure function of the
# VM's UUID, like derive_tap — reconstructible from the row with no allocator.

# First localhost TCP port for a migration tunnel's socat carrier. Kept clear of
# the NBD-export port window (nbd_port: 10000-14999) so a VM being migrated can
# run both at once without a collision.
MIGRATION_TUNNEL_PORT_BASE = 15000
MIGRATION_TUNNEL_PORT_SPAN = 5000

# The base for a migration tunnel's dedicated route-table id (§2.9.3). The
# target adds one `ip -6 rule from <vmv6> lookup <table>` per migrated VM, whose
# only route is `default dev <tunnel>` — this is what forces the guest's replies
# up the tunnel instead of out the target's own (spoof-dropped) uplink. Table 0
# is the unspec table and low ids are reserved (255 local, 254 main, 253
# default), so we sit the per-VM tables well clear of them.
MIGRATION_TABLE_BASE = 20000
MIGRATION_TABLE_SPAN = 40000

# WireGuard VPN broker (spec/19-vpn-broker.md). Each tunnel terminates on the
# host with its own wg interface; a per-server slot index gives each one a UDP
# listen port and a private overlay link, in the spirit of allocate_ipv6 /
# derive_ipv4_link. The slot SCAN lives with the VPN Tunnel controller (it
# queries the doctype); the derivations below are pure functions of the slot.

# First UDP port for tunnel listeners (WireGuard's default port). Slot 0 -> 51820,
# slot 1 -> 51821, … The host has no input firewall (the `inet atlas` table is
# forward + nat only), so the port is reachable on the host's public address.
TUNNEL_PORT_BASE = 51820

# Fixed ULA supernet for per-tunnel overlay links — the private v6 addresses the
# host and client ends of a tunnel carry so the VM has a return path. Like the
# NAT44 egress supernet, the overlay is private, routed into one interface, and
# never appears on the public wire, so it only has to be unique per host.
ATLAS_TUNNEL_SUPERNET = "fd00:a71a:5000::/48"


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
	octets = [hex_only[i : i + 2] for i in range(0, 8, 2)]
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


def cpu_weight(cpu_max_cores: float) -> int:
	"""The cgroup v2 `cpu.weight` carrying `cpu_max_cores` as a proportional share.

	Scales the bandwidth share by `CPU_WEIGHT_PER_CORE` (a full core -> the cgroup
	default 100, 1/16 core -> ~6) and clamps into the kernel's [1, 10000] range, so
	the weights of co-resident VMs sum to the same proportions placement reasons
	about in `cpu_max_cores` units."""
	scaled = round(cpu_max_cores * CPU_WEIGHT_PER_CORE)
	return max(CPU_WEIGHT_MIN, min(CPU_WEIGHT_MAX, scaled))


def cgroup_args(
	cpu_max_cores: float,
	memory_megabytes: int,
	disk_gigabytes: int,
	cpu_mode: str = CPU_MODE_HARD,
	vcpus: int = 1,
) -> list[str]:
	"""Jailer `--cgroup` flags bounding the VM's memory and CPU (cgroup v2).

	- `memory.max` = guest RAM + headroom (whole-process ceiling).
	- `memory.swap.max` = 0 — never swap guest RAM to host disk (also the
	  per-VM form of Firecracker's "disable swap / data-remanence" guidance).
	- CPU depends on `cpu_mode`. Both treat `cpu_max_cores` as the VM's share —
	  `cpu_max_cores` cores' worth of bandwidth per 100 ms period (a *bandwidth*
	  share, not cpuset pinning; distinct from `vcpus`, the guest `vcpu_count`).
	  Fractional for sub-1 sizes: 1/16 core is `6250 100000`.

	  - `CPU_MODE_HARD` (the default): `cpu.max = <cpu_max_cores * period>
	    <period>` and no `cpu.weight`. The share is also a hard ceiling — a 1/16
	    VM is throttled to 6.25% of a core *even on an idle host*. This is the
	    pre-existing behavior, emitted byte-for-byte.
	  - `CPU_MODE_RELAXED`: `cpu.weight = cpu_weight(cpu_max_cores)` (the
	    guaranteed proportional floor *under contention*) plus a loose `cpu.max =
	    <vcpus * period> <period>` burst ceiling. CFS is work-conserving for
	    weights, so the VM gets at least its share when the host is busy and
	    bursts into spare host CPU when it isn't — up to `vcpus` whole cores (a
	    sub-1 tier boots one vCPU thread, so it bursts to at most one core). The
	    ceiling keeps a single busy VM from monopolizing an idle host.

	`disk_gigabytes` is unused here — the VM disk is a thin LV bounded by
	pool-space accounting (the pool's `data_percent`, monitored at the host),
	not by any per-process limit. It is kept in the signature so the one call
	site passes the VM's full resource triple.
	"""
	_ = disk_gigabytes
	period_us = 100000
	memory_max_bytes = (memory_megabytes + MEMORY_HEADROOM_MIB) * 1024 * 1024
	args = [
		"--cgroup",
		f"memory.max={memory_max_bytes}",
		"--cgroup",
		"memory.swap.max=0",
	]
	if cpu_mode == CPU_MODE_RELAXED:
		# Weight = the guaranteed share under contention; cpu.max = a loose
		# whole-vcpu ceiling the VM may burst up to on an idle host.
		ceiling_us = round(vcpus * period_us)
		args += [
			"--cgroup",
			f"cpu.weight={cpu_weight(cpu_max_cores)}",
			"--cgroup",
			f"cpu.max={ceiling_us} {period_us}",
		]
	else:
		cpu_quota_us = round(cpu_max_cores * period_us)
		args += [
			"--cgroup",
			f"cpu.max={cpu_quota_us} {period_us}",
		]
	return args


def resource_limit_args(disk_gigabytes: int) -> list[str]:
	"""Jailer `--resource-limit` flags (setrlimit) bounding open files.

	The VM disk is an LVM thin volume (a block device), not a file the jailed
	process creates, so `RLIMIT_FSIZE` would not bound it — `fsize` only caps
	regular-file growth, and writes to a block device are not regular-file
	growth. We omit it: pool-space accounting (the thin pool's `data_percent`,
	monitored at the host) is the real disk-runaway guard, not a per-process
	file-size rlimit. `no-file` still bounds the descriptor count.

	`disk_gigabytes` is unused now (kept in the signature so the one call site
	passes the VM's full resource triple, matching `cgroup_args`).
	"""
	_ = disk_gigabytes
	return [
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


def derive_vm_tunnel(virtual_machine_name: str) -> str:
	"""mig6-<first 8 hex of the VM's UUID>. Length 13, IFNAMSIZ-safe (`mig6-` (5)
	+ 8 = 13). The migration tunnel's `tun` device name (spec/24 §2.9.1), keyed to
	the VM — one device per migrated VM, brought up at cutover and left up while
	the /128 is forwarded. Both hosts derive it identically, so teardown and
	lost-task re-entry need only the UUID, not stored state. Distinct from the
	`atlas-`/`wg-` device families so the three never collide."""
	hex_only = uuid.UUID(virtual_machine_name).hex
	return f"mig6-{hex_only[:8]}"


def derive_vm_tunnel_port(virtual_machine_name: str) -> int:
	"""A stable per-VM localhost TCP port for the migration tunnel's socat carrier,
	derived like nbd_port but in a non-overlapping window (§2.9.1) so a VM can run
	its NBD export and its forward tunnel at once without a collision."""
	index = int(uuid.UUID(virtual_machine_name).hex[:4], 16) % MIGRATION_TUNNEL_PORT_SPAN
	return MIGRATION_TUNNEL_PORT_BASE + index


def derive_vm_tunnel_table(virtual_machine_name: str) -> int:
	"""A stable per-VM route-table id for the migration return route (§2.9.3). One
	table per migrated VM holds a single `default dev <tunnel>` route; an
	`ip -6 rule from <vmv6>` selects it, forcing the guest's replies up the tunnel.
	Derived from the UUID so both the install (target-receive) and the teardown
	(collapse) name the same table with no stored state."""
	index = int(uuid.UUID(virtual_machine_name).hex[:8], 16) % MIGRATION_TABLE_SPAN
	return MIGRATION_TABLE_BASE + index


def derive_tunnel_interface(tunnel_name: str) -> str:
	"""wg-<first 11 hex of the tunnel UUID>. Length 14, IFNAMSIZ-safe (`wg-` (3) +
	11 = 14), and distinct from a VM's `atlas-…` tap/veth names. Pure function of
	the tunnel's UUID, like derive_tap — so the on-host interface is
	reconstructible from the row with no allocator."""
	hex_only = uuid.UUID(tunnel_name).hex
	return f"wg-{hex_only[:11]}"


def tunnel_listen_port(slot_index: int) -> int:
	"""The UDP port a tunnel's wg interface listens on: TUNNEL_PORT_BASE + slot."""
	return TUNNEL_PORT_BASE + slot_index


def tunnel_endpoint_address(server_name: str) -> str:
	"""The address a tunnel client dials — the single seam for the private-VPC
	future (spec/19-vpn-broker.md). Today the server's public IPv4, so an
	IPv4-only client can connect and reach the v6-only VM over the tunnel; later a
	private VPC address, swapped here with the Server's `transport`. Fails loud if
	the server has no v4 (a misconfigured/Self-Managed host without one)."""
	address = frappe.db.get_value("Server", server_name, "ipv4_address")
	if not address:
		raise frappe.ValidationError(f"Server {server_name} has no ipv4_address for a tunnel endpoint")
	return address


def allocate_tunnel_slot(server_name: str) -> int:
	"""Lowest unused per-server tunnel slot index. Scans the server's VPN Tunnel
	rows whose status is not Revoked — a Revoked tunnel has released its slot back
	to the pool (its port + overlay are free to reuse), exactly as a Terminated VM
	releases its /128. Locks the Server row for the scan so two concurrent requests
	cannot claim the same slot, mirroring allocate_ipv6.

	This row lock — not a DB `unique` index — is what makes slot allocation
	race-safe, and a static unique (server, slot_index) index would be *wrong*: a
	reused slot collides with the lingering Revoked row that still carries it (revoke
	keeps slot_index; it does not delete the row). Contrast the Firewall unique index
	on virtual_machine, which is safe only because remove_firewall deletes the row
	outright, leaving nothing to collide with."""
	frappe.get_doc("Server", server_name, for_update=True)
	used = {
		index
		for index in frappe.get_all(
			"VPN Tunnel",
			filters={"server": server_name, "status": ["!=", "Revoked"]},
			pluck="slot_index",
		)
		if index is not None
	}
	index = 0
	while index in used:
		index += 1
	return index


def tunnel_overlay_link(slot_index: int) -> tuple[str, str]:
	"""(host_side, client_side) /127 overlay CIDRs for a tunnel, indexed by its
	per-server slot. A point-to-point link inside ATLAS_TUNNEL_SUPERNET: the host
	end is the lower address (addresses the host's wg interface), the client end
	the upper (the address the VM routes its replies back to, carried in the
	client's wg `Address`). A /127 is the RFC 6164 point-to-point form — both
	addresses are usable. Mirrors derive_ipv4_link's per-host-unique allocation.

	Example: slot 0 -> ('fd00:a71a:5000::/127', 'fd00:a71a:5000::1/127')."""
	supernet = ipaddress.IPv6Network(ATLAS_TUNNEL_SUPERNET)
	base = int(supernet.network_address) + slot_index * 2
	link = ipaddress.IPv6Network((base, 127))
	if not supernet.supernet_of(link):
		raise frappe.ValidationError("No tunnel overlay capacity on server")
	hosts = list(link.hosts())
	return (
		f"{hosts[0]}/{link.prefixlen}",
		f"{hosts[1]}/{link.prefixlen}",
	)
