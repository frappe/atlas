from __future__ import annotations

import ipaddress

import frappe
from frappe import _
from frappe.utils.caching import redis_cache

from atlas.atlas.core.exceptions import AtlasUserError

FDAC_PREFIX = 0xFDAC
MESH_PREFIX = 0xFDAA
REGION_BITS = 16
TENANT_BITS = 32
CLIENT_BITS = 32
REGION_LIMIT = 1 << REGION_BITS
TENANT_LIMIT = 1 << TENANT_BITS
CLIENT_LIMIT = 1 << CLIENT_BITS


@redis_cache(ttl=36000)
def get_client_fdac(region_id: int, tenant_id: int, client_id: int) -> str:
	"""Return the fdac address of one WireGuard client."""
	region = _checked_int("Region ID", region_id, 0, REGION_LIMIT - 1)
	tenant = _checked_int("Tenant ID", tenant_id, 1, TENANT_LIMIT - 1)
	client = _checked_int("Client ID", client_id, 1, CLIENT_LIMIT - 1)
	address = (FDAC_PREFIX << 112) | (region << 96) | (tenant << 64) | client
	return str(ipaddress.IPv6Address(address))


def tenant_fdaa_prefix(region_id: int, tenant_id: int) -> str:
	"""Return the /64 of one tenant's private VM addresses."""
	region = _checked_int("Region ID", region_id, 0, REGION_LIMIT - 1)
	tenant = _checked_int("Tenant ID", tenant_id, 1, TENANT_LIMIT - 1)
	network = ipaddress.IPv6Network(((MESH_PREFIX << 112) | (region << 96) | (tenant << 64), 64))
	return str(network)


def _checked_int(label: str, value: object, low: int, high: int) -> int:
	"""Return value as an int inside its field, or reject the request."""
	if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
		frappe.throw(
			_("{0} must be an integer from {1} to {2}.").format(label, low, high),
			exc=AtlasUserError,
		)
		raise AssertionError
	return value
