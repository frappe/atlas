"""Networking helpers: IPv6 carve, MAC/tap derivation, IPv6 allocation, IPv4 egress link.

Also holds the jailer-isolation derivations — per-VM uid/gid, network-namespace
and veth-pair names, and the cgroup/rlimit argument strings. Like `derive_mac`
and `derive_tap`, these are pure functions of the VM's UUID (and, for the caps,
its own resource fields), so the on-host jail is fully reconstructible from the
Frappe row with no allocator and no extra DocType state.
"""

import hashlib
import hmac
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

# --- Private networking: the WireGuard HOST mesh -------------------------------
# The fabric that carries every VM's `fdaa::` private address across hosts, and
# gives each `Tenant` a /48 isolation boundary (see
# llm/references/private-networking-host-mesh.md). Every value below is a pure
# function of a UUID (the Tenant's, the VM's, or the Server's), so the whole
# overlay is reconstructible from the Frappe rows with NO allocator and NO extra
# DocType state — the same iron law as derive_mac / derive_tap / derive_ipv4_link.
#
#   fdaa : TTTT TTTT : RRRR : VVVV VVVV VVVV VVVV
#    16       32        16          64
#    ULA    HKDF(tenant) region     HKDF(vm UUID)
#
# The tenant /48 is the isolation boundary; the region hextet (bits 48-63) is the
# reserved 4th hextet (§D1 multi-region) — 0 for a single-region deployment, which
# makes the address identical to the design's §A4 layout, so a single-region VM
# reads `fdaa:T:T:0:V:V:V:V`. The VM part (bits 64-127) is host-INDEPENDENT, so a
# migrated VM keeps its private identity byte-for-byte (§2.1, §7).

# The fixed ULA tag. fd00::/8 is the ULA block; we pin fdaa::/16 to mirror fly.io's
# 6PN, leaving the rest of fd00::/8 free (and clear of ATLAS_TUNNEL_SUPERNET above).
PRIVATE_NETWORK_ULA = "fdaa::/16"

# Each tenant gets a /48 — the isolation boundary. After the 16-bit fdaa:: tag that
# leaves 32 bits of tenant id (2^32 tenants; per-pair collision ~= 2^-32).
TENANT_PREFIX_LENGTH = 48
TENANT_ID_BITS = TENANT_PREFIX_LENGTH - 16  # 32

# Bits 48-63 hold a 16-bit region id (65,536 regions; §D1). All-zero for a
# single-region deployment. Region is a placement decision fixed for the VM's life,
# so — unlike host bits (§A3/§B4) — it freezes at creation and never contradicts a
# migrated VM.
REGION_BITS_OFFSET = 128 - 64  # 64: region occupies bits 48-63, above the VM part
REGION_ID_BITS = 16

# A VM's address inside the tenant /48 uses a 64-bit host-part derived from the VM
# UUID (NOT the per-host allocate_ipv6 index, which collides across hosts). 64 bits
# -> collision ~= 2^-64, birthday-safe past any tenant's VM count. Host-INDEPENDENT.
VM_HOST_PART_BITS = 64

# The reserved infra /48 (all-zero tenant bits, never HKDF-derivable for a real
# Tenant). The proxy's tap and each host's own mesh address live here (§2.4, §6).
INFRA_PREFIX = "fdaa:0:0::/48"

# WireGuard overlay MTU — wg adds ~80 bytes over a 1500 path, so wg-mesh (and the
# guest eth0) must be pinned or large packets blackhole (§2.3, §5). Proven clean at
# exactly 1420 on the real Scaleway hosts (Phase-0 gate #4).
WIREGUARD_MTU = 1420

# The fixed UDP port the HOST wg-mesh listens on, region-wide (§3).
WG_HOST_PORT = 51820

# The customer gateway (spec/25 Phase 5, spec/26). A customer's laptop lands as a
# /128 INSIDE its tenant's /48 — a real address the mesh already routes — so the
# tenant's VMs treat it exactly like a sibling VM and the return path is automatic.
# The 4th hextet (bits 48-63, the same hextet region uses) is structurally 0x0000
# for a VM; a CLIENT sets it to 0x0001 and derives its low 48 bits from the peer-row
# UUID. Clients and VMs are therefore disjoint sub-ranges of the same /48 BY
# CONSTRUCTION — no allocator, no collision (reference §3). Because the client shares
# the tenant /48 bits, `client & /48 == tenant prefix`, the identity the gateway's
# same_48 eBPF guard leans on.
CLIENT_HEXTET = 0x0001  # 4th hextet marks a customer client; VMs are 0x0000
CLIENT_HOST_PART_BITS = 48  # low 48 bits derived from the peer-row UUID (birthday-safe)

# The fixed UDP port the gateway VM's single wg0 listens on, shared by every peer
# (reference §4). Same fixed WireGuard port as the host mesh and the Central tunnel,
# so the management-firewall's one `udp dport 51820 accept` already covers it.
WG_GATEWAY_PORT = WG_HOST_PORT

# HKDF "info" / domain-separation labels. Distinct labels so the same UUID seeds
# independent values (never reuse a derived secret across purposes).
_INFO_TENANT_PREFIX = b"atlas-private-tenant-prefix-v1"
_INFO_VM_HOST_PART = b"atlas-private-vm-host-part-v1"
_INFO_HOST_WIREGUARD_KEY = b"atlas-host-wg-v1"
_INFO_HOST_MESH_INDEX = b"atlas-host-mesh-index-v1"
_INFO_CLIENT_HOST_PART = b"atlas-vpc-client-host-part-v1"


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
	"""Lowest unused address in the server's VM range.

	Skips ::0 (subnet id) and ::1 (host). A VM in status Terminated has
	released its address back into the pool — only live (non-Terminated)
	VMs count as occupying an address.

	"Used" is scoped to the RANGE, not the server: a keep-address migration
	carries a VM's /128 onto a different host UNCHANGED (spec/24), so a live VM
	sitting on server B can still own an address that falls inside server A's
	range (its birth range). Filtering only by `server == server_name` would miss
	that VM and hand its address to a new provision here — the exact double-alloc
	that made two VMs share `…:b3::3` across two hosts. So we consider every live
	VM whose address is inside THIS range, regardless of which server it now runs
	on. (Two hosts must not share a range for this to be sufficient; each
	Scaleway host gets its own /64, so a range uniquely identifies its birth host.)
	"""
	server = frappe.get_doc("Server", server_name, for_update=True)
	network = ipaddress.IPv6Network(server.ipv6_virtual_machine_range)
	used = {
		str(ipaddress.IPv6Address(address))
		for address in frappe.get_all(
			"Virtual Machine",
			filters={"status": ["!=", "Terminated"]},
			pluck="ipv6_address",
		)
		if address and ipaddress.IPv6Address(address) in network
	}
	for index, candidate in enumerate(network.hosts()):
		# IPv6Network.hosts() already excludes ::0 (subnet anycast); we additionally
		# skip ::1, which the host (server) uses. Allocation starts at ::2.
		if index < 1:
			continue
		if str(candidate) not in used:
			return str(candidate)
	raise frappe.ValidationError("No IPv6 capacity on server")


def address_is_free_on_server(server_name: str, address: str, ignore_vm: str | None = None) -> bool:
	"""Whether `address` is unclaimed by a live (non-Terminated) VM on `server_name`.

	The collision gate for the keep-address migration path: unlike change-address,
	which calls allocate_ipv6 (guaranteed-free by construction), keep-address carries
	the VM's existing /128 onto the target UNCHANGED — so it must independently verify
	the target isn't already hosting a different VM on that same /128 (two VMs sharing
	a /128 on one host is unrecoverable-by-routing; the host's single `<vmv6>/128 dev
	<veth>` route can only point at one veth). `ignore_vm` excludes the migrating VM's
	OWN row, which on a resume/re-entry may already be denormalized onto the target.
	Normalizes both sides through IPv6Address so `::2` and `0:0:…:2` compare equal."""
	wanted = str(ipaddress.IPv6Address(address))
	filters = {"server": server_name, "status": ["!=", "Terminated"]}
	if ignore_vm:
		filters["name"] = ["!=", ignore_vm]
	for held in frappe.get_all("Virtual Machine", filters=filters, pluck="ipv6_address"):
		if held and str(ipaddress.IPv6Address(held)) == wanted:
			return False
	return True


def derive_guest_link_local(virtual_machine_name: str) -> str:
	"""The guest eth0's IPv6 link-local address, EUI-64-derived from the VM's MAC (which is
	itself `derive_mac`). The guest auto-configures `fe80::<eui64>` on eth0 from its MAC, so
	this is a pure function of the VM UUID — no probing.

	Used by the customer gateway (spec/26): to deliver a FORWARDED /128 (a customer client's
	address the guest does not own) into the gateway guest, the host netns must route it
	`via <this link-local> dev <tap>` — the guest answers ND for its own link-local and then
	forwards the packet on (eth0 → wg0). A plain `dev <tap>` route has no ND neighbor for a
	non-owned address and loops. Verified on a real host.

	EUI-64: split the 48-bit MAC, insert `ff:fe` in the middle, and flip the U/L bit (bit 1
	of the first octet). `derive_mac` yields `06:00:aa:bb:cc:dd`, so the first octet 0x06
	flips to 0x04 → `fe80::0400:aaff:febb:ccdd`."""
	mac = derive_mac(virtual_machine_name)  # 06:00:aa:bb:cc:dd
	octets = [int(part, 16) for part in mac.split(":")]
	octets[0] ^= 0x02  # flip the U/L bit
	eui64 = [*octets[:3], 0xFF, 0xFE, *octets[3:]]
	words = [f"{eui64[i]:02x}{eui64[i + 1]:02x}" for i in range(0, 8, 2)]
	return str(ipaddress.IPv6Address("fe80::" + ":".join(words)))


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


def derive_ipv4_link(ipv6_address: str | None = None, *, index: int | None = None) -> tuple[str, str]:
	"""(host_side, guest_side) /30 CIDRs for a VM's private NAT44 egress link.

	The guest's private IPv4 is masqueraded at the host uplink and never seen
	on the wire, so it only needs to be unique per host. Each VM gets a
	point-to-point /30 inside `IPV4_EGRESS_SUPERNET`, indexed either by:

	  - the low 14 bits of a v6 `ipv6_address` (the default, backward-compatible
	    path). For a PUBLIC VM this is the DO/124's low bits (indices 2..15,
	    per-host unique by the v6 allocator); a larger Self-Managed range stays
	    unique as long as it fits the /16 (16384 /30 links). Mirrors the v6 host
	    part so one VM's v4 and v6 share an index — easy to correlate in `ip addr`.
	  - an explicit `index` — the path a **dark** VM takes (§6). A dark VM has NO
	    public `ipv6_address`, and its private address's low bits are HKDF-derived,
	    so they are collision-free only statistically, not guaranteed per-host
	    unique. The caller passes the VM's allocated slot (the same
	    per-host-unique index `allocate_ipv6` hands out) so two dark VMs on one
	    host never share a /30.

	Exactly one of `ipv6_address` / `index` must be given.

	Example: ::2 -> ('100.64.0.9/30', '100.64.0.10/30').
	"""
	if (ipv6_address is None) == (index is None):
		raise frappe.ValidationError("derive_ipv4_link: pass exactly one of ipv6_address= or index=")
	if index is None:
		index = int(ipaddress.IPv6Address(ipv6_address)) & 0x3FFF
	supernet = ipaddress.IPv4Network(IPV4_EGRESS_SUPERNET)
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


# --- Private networking: derivations for the WireGuard HOST mesh ---------------


def _hkdf(seed: bytes, info: bytes, length: int) -> bytes:
	"""HKDF-SHA256 (extract + expand), enough for our short outputs.

	Stdlib-only (operating principle #5). We never need more than 32 bytes, so a
	single expand block suffices. Distinct `info` labels domain-separate the
	tenant prefix, VM host-part, host wg key, and host mesh index from one UUID.
	"""
	if length > 32:
		raise ValueError("this minimal HKDF emits at most one SHA256 block (32 bytes)")
	pseudorandom_key = hmac.new(b"atlas-private-network", seed, hashlib.sha256).digest()
	block = hmac.new(pseudorandom_key, info + b"\x01", hashlib.sha256).digest()
	return block[:length]


def _name_seed(name: str) -> bytes:
	"""The HKDF seed for a resource name. A `Server` / `Virtual Machine` name is always
	a UUID (autoname → str(uuid4())), so we use its 16 raw bytes — byte-for-byte stable.
	A `Tenant` name, however, IS the Central `Team.name` (e.g. `TEAM-00001`, a naming
	series — NOT a UUID; see doctype/tenant/tenant.py), so `uuid.UUID(name)` would crash
	on every real tenant. Fall back to the name's UTF-8 bytes for any non-UUID id. The
	seed feeds HKDF either way, so the derived address stays deterministic + host-
	independent; only the seed encoding differs by name shape."""
	try:
		return uuid.UUID(name).bytes
	except (ValueError, AttributeError, TypeError):
		return name.encode("utf-8")


def derive_tenant_prefix(tenant_name: str) -> str:
	"""The tenant's /48 ULA prefix, e.g. 'fdaa:1a2b:3c4d::/48'.

	Pure function of the Tenant name: 16-bit fdaa:: tag + 32 derived tenant bits. No
	allocator, no registry row — recomputed from the Tenant name wherever it is needed
	(provision, reconcile, DNS), exactly like derive_mac. The tenant /48 is the isolation
	boundary (§2.1) and is preserved across regions (§D1). The Tenant name is the Central
	Team id (a naming series, not a UUID), so the seed is derived via `_name_seed`."""
	tenant_id = int.from_bytes(_hkdf(_name_seed(tenant_name), _INFO_TENANT_PREFIX, 4), "big")
	tenant_id &= (1 << TENANT_ID_BITS) - 1
	ula = ipaddress.IPv6Network(PRIVATE_NETWORK_ULA)
	# Place the 32-bit tenant id immediately below the 16-bit ULA tag.
	base = int(ula.network_address) | (tenant_id << (128 - TENANT_PREFIX_LENGTH))
	return str(ipaddress.IPv6Network((base, TENANT_PREFIX_LENGTH)))


def derive_private_address(tenant_name: str, virtual_machine_name: str, region_index: int = 0) -> str:
	"""The VM's private overlay address inside its tenant's /48, e.g.
	'fdaa:1a2b:3c4d:0:9f3e:1100:abcd:0042'.

	The host-part is 64 bits derived from the VM UUID (§2.1) so two of a tenant's
	VMs on different hosts never collide, and it is host-INDEPENDENT — a pure
	function of (tenant, vm), so it survives migration byte-for-byte (§7). The
	16-bit `region_index` (§D1) fills the reserved 4th hextet (bits 48-63); it
	defaults to 0, giving the single-region layout `fdaa:T:T:0:V:V:V:V` (the 4th
	hextet reads `0`), so an existing single-region address is unchanged."""
	if not 0 <= region_index < (1 << REGION_ID_BITS):
		raise ValueError(f"region_index {region_index} out of range for {REGION_ID_BITS} bits")
	prefix = ipaddress.IPv6Network(derive_tenant_prefix(tenant_name))
	# A VM name is always a UUID (the 64-bit birthday-safety math assumes it), but route
	# through _name_seed so a non-UUID id never crashes the derivation — same as tenant.
	host_part = int.from_bytes(_hkdf(_name_seed(virtual_machine_name), _INFO_VM_HOST_PART, 8), "big")
	host_part &= (1 << VM_HOST_PART_BITS) - 1
	region_bits = region_index << REGION_BITS_OFFSET
	address = int(prefix.network_address) | region_bits | host_part
	candidate = ipaddress.IPv6Address(address)
	if candidate not in prefix:
		# Unreachable given the bit budget, but fail loud rather than hand out an
		# out-of-prefix address.
		raise ValueError(f"{candidate} fell outside tenant prefix {prefix}")
	return str(candidate)


def _clamp_curve25519_scalar(scalar: bytearray) -> bytearray:
	"""Clamp 32 bytes into a valid Curve25519 private scalar (RFC 7748)."""
	scalar[0] &= 248
	scalar[31] &= 127
	scalar[31] |= 64
	return scalar


def derive_host_wireguard_keypair(server_name: str) -> tuple[str, str]:
	"""(private_key_b64, public_key_b64) for a HOST's wg-mesh device, derived from
	the Server UUID (§3, §8).

	Variant (b) puts WireGuard on the HOST, not the guest — so this keys off the
	*Server* UUID. Derived (not stored) so the entire desired mesh reconstructs from
	the Server table: a re-bootstrap or rebuild re-derives the SAME identity with
	zero peer churn, and the controller can compute every host's PUBLIC key from its
	UUID alone (what lets `Server.wireguard_public_key` be a derived denorm). The
	private key is injected to the host at bootstrap via the root-SSH layer, never on
	an argv.

	The public key is the real Curve25519 base-point multiply (via `cryptography`, a
	direct frappe dependency — not a new one). Verified byte-for-byte against `wg
	pubkey` on a real Scaleway host.

	Cost (§10/#1): derivation is secret-equivalent — anyone who can derive can
	recompute any host's private key, the same trust class as the Atlas root SSH key.
	"""
	import base64

	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

	seed = _hkdf(uuid.UUID(server_name).bytes, _INFO_HOST_WIREGUARD_KEY, 32)
	private_scalar = bytes(_clamp_curve25519_scalar(bytearray(seed)))
	private_key = X25519PrivateKey.from_private_bytes(private_scalar)
	public_raw = private_key.public_key().public_bytes(
		serialization.Encoding.Raw, serialization.PublicFormat.Raw
	)
	return base64.b64encode(private_scalar).decode(), base64.b64encode(public_raw).decode()


def derive_host_mesh_address(server_name: str) -> str:
	"""The host's OWN endpoint on the mesh bus: `fdaa:0:0:<host-idx>::1` (§2.4).

	Lives in the infra /48 (`fdaa:0:0::/48`) — the same reserved, never-tenant-
	derivable prefix the proxy's tap uses — distinguished from the proxy by the
	16-bit host index derived from the Server UUID (bits 48-63). Assigned to the
	`wg-mesh` device in the host root netns (never on a veth), so it is reachable
	only from another host across the tunnel (§4c). Derived, not stored — the same
	HKDF-from-Server-UUID discipline as the host wg key.

	The host↔host bus (migration NBD, snapshot replication, image fan-out) dials
	this address so those bytes ride inside the tunnel, not on the public wire.

	Example: 'fdaa:0:0:a1b2::1'."""
	infra = ipaddress.IPv6Network(INFRA_PREFIX)
	host_index = int.from_bytes(_hkdf(uuid.UUID(server_name).bytes, _INFO_HOST_MESH_INDEX, 2), "big")
	host_index &= (1 << REGION_ID_BITS) - 1
	# The index sits in bits 48-63 (the same hextet region uses for a VM); the low
	# ::1 marks the host's own address, distinct from the all-zero infra network id.
	address = int(infra.network_address) | (host_index << REGION_BITS_OFFSET) | 1
	return str(ipaddress.IPv6Address(address))


def generate_host_signing_keypair() -> tuple[str, str]:
	"""Random ed25519 signing keypair for a HOST's ANCP envelope + record
	signatures (spec/31 §19.3 / §19.4). NOT derived from the Server UUID —
	a derived signing key's seed would be public (the UUID is on every
	Membership Record), defeating the purpose. Generated ONCE at first
	`Server.validate`; the priv lives only on the host after
	`_write_ancp_bootstrap_state` pushes it (the controller never persists
	the priv; a re-Bootstrap reads the existing files from the host — the
	host already has them).

	Returns `(priv_b64, pub_b64)` — base64-standard for both halves, the
	shape the host's `/etc/atlas-networkd/signing-{private,public}-key` files
	hold (one line, stripped).
	"""
	import base64

	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	priv_raw = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()


def derive_client_address(tenant_name: str, client_peer_name: str) -> str:
	"""A customer client's /128 inside the tenant /48 (spec/25 Phase 5, spec/26), e.g.
	'fdaa:1a2b:3c4d:1:abcd:ef01:2345'.

	The customer's laptop is modelled as a "dark VM at the customer's premises": a real
	address inside the tenant's /48, so the tenant's VMs route to it exactly like a
	sibling VM and the return path is automatic (reference §3, §5.2). It is:

	  - HOST-INDEPENDENT and reconstructible from the row — a pure function of
	    (tenant, peer), the same discipline as derive_private_address, so the laptop
	    keeps its VPC address regardless of which gateway terminates it;
	  - COLLISION-FREE vs. VM addresses by construction — the 4th hextet is 0x0001 for a
	    client and 0x0000 for a VM, so the two are disjoint sub-ranges of the same /48
	    with no allocator;
	  - inside the tenant /48, so `client & /48 == derive_tenant_prefix(tenant)` — the
	    identity the gateway's static same_48 guard leans on to confine the destination.

	48-bit host-part ⇒ per-tenant collision ≈ n²/2⁴⁹ (birthday-safe past any tenant's
	device count). The peer name is a UUID (VPN Peer autoname=hash), but route
	through _name_seed so a non-UUID id never crashes the derivation — same as tenant/VM."""
	prefix = ipaddress.IPv6Network(derive_tenant_prefix(tenant_name))
	host_part = int.from_bytes(_hkdf(_name_seed(client_peer_name), _INFO_CLIENT_HOST_PART, 6), "big")
	host_part &= (1 << CLIENT_HOST_PART_BITS) - 1
	address = (
		int(prefix.network_address)
		| (CLIENT_HEXTET << REGION_BITS_OFFSET)  # 4th hextet = 0x0001, marks a client
		| host_part  # low 48 bits from the peer UUID
	)
	candidate = ipaddress.IPv6Address(address)
	if candidate not in prefix:
		# Unreachable given the bit budget (48-bit host-part + one hextet, both inside
		# the /48), but fail loud rather than hand out an out-of-prefix address.
		raise ValueError(f"{candidate} fell outside tenant prefix {prefix}")
	return str(candidate)
