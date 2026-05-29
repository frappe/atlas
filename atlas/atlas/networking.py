"""Networking helpers: IPv6 carve, MAC/tap derivation, IPv6 allocation, IPv4 egress link."""

import ipaddress
import uuid

import frappe

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
