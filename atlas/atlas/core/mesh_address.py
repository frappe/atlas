from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, cast
from uuid import UUID

import frappe
from frappe import _
from frappe.model.document import Document

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


MESH_PREFIX = 0xFDAA
MESH_NETWORK = ipaddress.IPv6Network((MESH_PREFIX << 112, 16))
# A VM address is fdaa | region 16 | tenant 32 | padding 48 | VM number 16. The padding is zero.
MAXIMUM_VIRTUAL_MACHINE_NUMBER = 0xFFFF
# Atlas fills the padding and VM number, so no VM can use its address.
ATLAS_VIRTUAL_MACHINE_NUMBER = (1 << 64) - 1
WIREGUARD_PREFIX = 0xFDAB
# The prefix and the region take the first 32 bits, so the low 96 bits of the UUID are the peer part.
WIREGUARD_PEER_MASK = (1 << 96) - 1


def validate_region_id(region_id: int) -> None:
	"""Refuse a region ID that does not fit in one IPv6 field."""
	if not 0 <= region_id <= 0xFFFF:
		frappe.throw(_("Atlas Settings region ID must be a 16-bit unsigned integer."))


def get_virtual_machine_mesh_address(
	virtual_machine: Document | frappe._dict, region_id: int | None = None
) -> str:
	"""Return the mesh address from stable Atlas request metadata. A caller in a loop passes the region."""
	if region_id is None:
		region_id = cast("AtlasSettings", frappe.get_single("Atlas Settings")).region_id
	validate_region_id(region_id)

	virtual_machine_name = cast(str, virtual_machine.name)
	virtual_machine_number = int(virtual_machine_name.rsplit("-", 1)[-1])
	if virtual_machine_number > MAXIMUM_VIRTUAL_MACHINE_NUMBER:
		frappe.throw(
			_("Virtual Machine number {0} is too large for a mesh address.").format(virtual_machine_number)
		)

	address = (
		(MESH_PREFIX << 112) | (region_id << 96) | (virtual_machine.tenant_id << 64) | virtual_machine_number
	)
	return str(ipaddress.IPv6Address(address))


def get_atlas_mesh_address(region_id: int) -> str:
	"""Return the tenant-0 mesh address of Atlas in one region."""
	validate_region_id(region_id)

	return str(ipaddress.IPv6Address((MESH_PREFIX << 112) | (region_id << 96) | ATLAS_VIRTUAL_MACHINE_NUMBER))


def get_region_mesh_address_prefix(region_id: int) -> str:
	"""Return the leading hextets for VM mesh addresses in one region."""
	validate_region_id(region_id)

	return f"{MESH_PREFIX:x}:{region_id:x}"


def get_wireguard_ip_address(peer_id: UUID, region_id: int) -> str:
	"""Return the fdab::/16 wg0 address of one host or Atlas peer."""
	validate_region_id(region_id)

	address = (WIREGUARD_PREFIX << 112) | (region_id << 96) | (peer_id.int & WIREGUARD_PEER_MASK)
	return str(ipaddress.IPv6Address(address))
