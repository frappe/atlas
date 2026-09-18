from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.metal_server.core.mesh_peers import get_mesh_peer_file_contents, get_mesh_peers


def server_rows(*addresses: str) -> list[SimpleNamespace]:
	"""Return the Metal Server rows that frappe.get_all would return."""
	return [SimpleNamespace(private_ipv4_address=address) for address in addresses]


class TestMeshPeerFile(UnitTestCase):
	def test_every_running_address_becomes_one_line(self) -> None:
		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=server_rows("10.20.0.12", "10.20.0.11"),
			),
		):
			self.assertEqual(get_mesh_peer_file_contents(), "10.20.0.11\n10.20.0.12\n")

	def test_mesh_peers_returns_the_addresses_in_ascending_order(self) -> None:
		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=server_rows("10.20.0.12", "10.20.0.11"),
			),
		):
			self.assertEqual(get_mesh_peers(), ["10.20.0.11", "10.20.0.12"])

	def test_an_empty_address_is_skipped(self) -> None:
		rows = server_rows("10.20.0.11", "")

		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=rows,
			),
		):
			self.assertEqual(get_mesh_peer_file_contents(), "10.20.0.11\n")

	def test_a_duplicate_address_appears_once(self) -> None:
		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=server_rows("10.20.0.11", "10.20.0.11"),
			),
		):
			self.assertEqual(get_mesh_peer_file_contents(), "10.20.0.11\n")

	def test_an_invalid_address_is_rejected(self) -> None:
		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=server_rows("fdab::1"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			get_mesh_peer_file_contents()

	def test_an_empty_region_returns_no_contents(self) -> None:
		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=[],
			),
		):
			self.assertEqual(get_mesh_peer_file_contents(), "")

	def test_an_empty_region_returns_no_peers(self) -> None:
		with (
			patch(
				"atlas.metal_server.core.mesh_peers.frappe.get_all",
				return_value=[],
			),
		):
			self.assertEqual(get_mesh_peers(), [])
