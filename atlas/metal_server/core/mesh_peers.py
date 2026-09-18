from __future__ import annotations

import ipaddress

import frappe
from frappe import _

# Only a running host can answer unicast NDP transport.
PEER_SERVER_STATUSES = ("Running",)


def get_mesh_peers() -> list[str]:
	"""Return the unicast peer addresses for this region.

	Every running Metal Server with a private IPv4 address becomes one peer, in ascending address order. The unicast daemon drops the local address when it loads the list, so the same set suits every host.
	"""
	rows = frappe.get_all(
		"Metal Server",
		filters={"status": ("in", PEER_SERVER_STATUSES)},
		fields=["private_ipv4_address"],
	)

	peers: set[str] = set()
	for row in rows:
		if not row.private_ipv4_address:
			continue

		try:
			address = ipaddress.IPv4Address(row.private_ipv4_address)
		except ipaddress.AddressValueError:
			frappe.throw(
				_("Metal Server holds an invalid private IPv4 address {0}.").format(row.private_ipv4_address)
			)
		peers.add(address.compressed)

	return sorted(peers, key=ipaddress.IPv4Address)


def get_mesh_peer_file_contents() -> str:
	"""Return the unicast peer file contents for this region."""
	peers = get_mesh_peers()
	if not peers:
		return ""

	return "\n".join(peers) + "\n"
