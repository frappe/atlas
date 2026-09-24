# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document


class WireGuardGatewayPeer(Document):
	"""One Central-provided WireGuard client of one gateway. Atlas is the source of truth."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		client_id: DF.Int | None
		fdac_address: DF.Data | None
		gateway: DF.Link | None
		public_key: DF.Data | None
		tenant_id: DF.Int | None
	# end: auto-generated types

	def validate(self) -> None:
		"""Reject records created or changed outside the WireGuard Gateway API."""
		if not getattr(self.flags, "managed_by_wg_gateway_api", False):
			frappe.throw(_("Manage WireGuard peers with the WireGuard Gateway API, not in Desk."))

	def on_trash(self) -> None:
		"""Refuse deletion outside the WireGuard Gateway API, so every delete syncs."""
		if not getattr(self.flags, "managed_by_wg_gateway_api", False):
			frappe.throw(_("Manage WireGuard peers with the WireGuard Gateway API, not in Desk."))


@frappe.whitelist(methods=["POST"])
def add_peer(gateway: str, tenant_id: int, client_id: int, public_key: str) -> dict[str, str | int]:
	"""Add one client to a gateway. Called by Central."""
	_validate_system_manager()
	from atlas.service.core.wg_gateway.peers import add_peer as add

	return add(gateway, tenant_id, client_id, public_key)


@frappe.whitelist(methods=["POST"])
def delete_peer(
	name: str | None = None, gateway: str | None = None, public_key: str | None = None
) -> dict[str, str | bool]:
	"""Delete one peer by name or by gateway and public key. Called by Central."""
	_validate_system_manager()
	from atlas.service.core.wg_gateway.peers import delete_peer as delete

	return delete(name=name, gateway=gateway, public_key=public_key)


@frappe.whitelist()
def list_peers(gateway: str, tenant_id: int | None = None) -> list[dict[str, Any]]:
	"""Return the peers of one gateway. Called by Central."""
	_validate_system_manager()
	from atlas.service.core.wg_gateway.peers import list_peers as list_all

	return list_all(gateway, tenant_id)


@frappe.whitelist(methods=["POST"])
def replace_peer_list(gateway: str, peers: list[dict[str, Any]]) -> dict[str, str | int]:
	"""Replace the complete peer list of one gateway. Called by Central."""
	_validate_system_manager()
	from atlas.service.core.wg_gateway.peers import replace_peer_list as replace

	peers = frappe.parse_json(peers) if isinstance(peers, str) else peers
	return replace(gateway, peers)


@frappe.whitelist()
def get_wireguard_config(gateway: str) -> dict[str, Any]:
	"""Return the connection bundle for one gateway. Called by Central."""
	_validate_system_manager()
	from atlas.service.core.wg_gateway.peers import get_wireguard_config as get_config

	return get_config(gateway)


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	user_type = frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type != "System User":
		frappe.throw(_("Only System Users can manage WireGuard Gateway peers."), frappe.PermissionError)
