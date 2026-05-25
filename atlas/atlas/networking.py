"""Networking helpers: IPv6 carve, MAC/tap derivation, IPv6 allocation."""

import ipaddress
import uuid


def carve_virtual_machine_range(prefix_cidr: str) -> str:
	"""Return the first /124 of the given /64.

	DigitalOcean assigns a /64 to each droplet but only the first /124 is
	routable inside DO's fabric. We hand out addresses inside that /124 only.
	"""
	network = ipaddress.IPv6Network(prefix_cidr, strict=False)
	first_124 = ipaddress.IPv6Network(f"{network.network_address}/124", strict=False)
	return str(first_124)


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

	Skips ::0 (subnet id) and ::1 (host). Considers Archived VMs as still
	holding their address — we do not reuse.
	"""
	import frappe  # noqa: PLC0415
	server = frappe.get_doc("Server", server_name, for_update=True)
	network = ipaddress.IPv6Network(server.ipv6_virtual_machine_range)
	used = {
		str(ipaddress.IPv6Address(address))
		for address in frappe.get_all(
			"Virtual Machine",
			filters={"server": server_name},
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
