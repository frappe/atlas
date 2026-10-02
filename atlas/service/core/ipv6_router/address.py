from __future__ import annotations

import ipaddress

import frappe
from frappe import _
from frappe.utils.caching import redis_cache

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.models import IPV6_INTERNET_DESTINATION

# Must match bpf/address.h in services/ipv6-router.
TENANT_BITS = 32
MACHINE_BITS = 16
MAXIMUM_PREFIX_LENGTH = 128 - TENANT_BITS - MACHINE_BITS


@redis_cache(ttl=36000)
def get_routed_ipv6(prefix: str, mesh_address: str) -> str:
	"""Return the public address that the IPv6 router maps to one mesh address."""
	network = parse_ipv6_network(prefix)
	validate_router_network(network)

	try:
		mesh = int(ipaddress.IPv6Address(mesh_address))
	except ValueError as error:
		frappe.throw(
			_("Mesh address {0} is not a valid IPv6 address.").format(mesh_address), exc=AtlasUserError
		)
		raise AssertionError from error

	tenant = (mesh >> 64) & 0xFFFFFFFF
	machine = mesh & 0xFFFFFFFFFFFFFFFF
	if machine >= 1 << MACHINE_BITS:
		frappe.throw(
			_("Mesh address {0} has padding that the routed IPv6 layout cannot hold.").format(mesh_address),
			exc=AtlasUserError,
		)

	return str(ipaddress.IPv6Address(int(network.network_address) | tenant << MACHINE_BITS | machine))


def parse_ipv6_network(prefix: str) -> ipaddress.IPv6Network:
	"""Return a canonical IPv6 network or reject the invalid prefix."""
	try:
		return ipaddress.IPv6Network(prefix, strict=True)
	except (TypeError, ValueError) as error:
		frappe.throw(_("IPv6 block {0} is not a canonical IPv6 prefix.").format(prefix), exc=AtlasUserError)
		raise AssertionError from error


def validate_router_network(network: ipaddress.IPv6Network) -> None:
	"""Require a global-unicast block with space for the tenant and VM fields."""
	if not network.subnet_of(ipaddress.IPv6Network(IPV6_INTERNET_DESTINATION)):
		frappe.throw(
			_("IPv6 router block {0} must be within {1}.").format(network, IPV6_INTERNET_DESTINATION),
			exc=AtlasUserError,
		)

	if network.prefixlen > MAXIMUM_PREFIX_LENGTH:
		frappe.throw(
			_("The IPv6 router needs a /{0} or larger block, not {1}.").format(
				MAXIMUM_PREFIX_LENGTH, network
			),
			exc=AtlasUserError,
		)
