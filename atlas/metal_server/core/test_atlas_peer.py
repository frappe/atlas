from __future__ import annotations

import base64
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from frappe.tests import UnitTestCase

from atlas.metal_server.core import atlas_peer as atlas_peer_module
from atlas.metal_server.core.atlas_peer import AtlasPeer

HOST = frappe._dict(
	name="host-1",
	wireguard_public_key="host-key",
	wireguard_ip_address="fdab:1::7",
	port=51820,
	endpoint_address="10.1.0.7",
)
PROXY = frappe._dict(name="vm-0000082", tenant_id=0, server="host-1")


class TestAtlasPeer(UnitTestCase):
	def setUp(self) -> None:
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = Path(directory.name) / "wireguard"
		self.passwords: dict[str, str] = {}
		patcher = patch.object(AtlasPeer, "directory", new_callable=PropertyMock, return_value=self.directory)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_identity_is_a_stored_private_key_and_a_tenant_zero_address(self) -> None:
		"""The key lives in Atlas Settings, so a site backup can restore it."""
		atlas_peer = AtlasPeer(self.settings())

		atlas_peer.ensure_identity()

		private_key = X25519PrivateKey.from_private_bytes(
			base64.b64decode(self.passwords["wireguard_private_key"])
		)
		public_key = private_key.public_key().public_bytes(
			serialization.Encoding.Raw, serialization.PublicFormat.Raw
		)
		self.assertEqual(atlas_peer.settings.wireguard_public_key, base64.b64encode(public_key).decode())
		self.assertEqual(atlas_peer.settings.wireguard_ip_address, "fdaa:1::ffff:ffff:ffff:ffff")

	def test_identity_is_created_once(self) -> None:
		atlas_peer = AtlasPeer(self.settings())
		atlas_peer.ensure_identity()
		public_key = atlas_peer.settings.wireguard_public_key

		atlas_peer.ensure_identity()

		self.assertEqual(atlas_peer.settings.wireguard_public_key, public_key)

	def test_config_routes_hosts_and_their_tenant_zero_vms(self) -> None:
		atlas_peer = AtlasPeer(self.settings())
		atlas_peer.ensure_identity()

		with self.records():
			config = atlas_peer.get_config()

		self.assertIn(f"PrivateKey = {self.passwords['wireguard_private_key']}\n", config)
		self.assertIn("Address = fdaa:1::ffff:ffff:ffff:ffff/128", config)
		self.assertIn("PostUp = ip -6 route replace fdab:1::/32 dev %i", config)
		self.assertIn("PostUp = ip -6 route replace fdaa:1::/64 dev %i", config)
		self.assertIn(
			"[Peer]\nPublicKey = host-key\nAllowedIPs = fdab:1::7/128, fdaa:1::52/128\nEndpoint = 10.1.0.7:51820\n",
			config,
		)

	def test_a_host_with_the_atlas_key_is_rejected(self) -> None:
		atlas_peer = AtlasPeer(self.settings(wireguard_public_key="host-key"))

		with (
			patch.object(atlas_peer_module.frappe, "get_all", return_value=[HOST]),
			self.assertRaisesRegex(frappe.ValidationError, "uses the Atlas WireGuard public key"),
		):
			atlas_peer.get_host_peers()

	def test_an_unchanged_config_is_not_written_again(self) -> None:
		atlas_peer = AtlasPeer(self.settings())
		atlas_peer.ensure_identity()

		with self.records():
			self.assertTrue(atlas_peer.write_config())
			self.assertFalse(atlas_peer.write_config())

		self.assertEqual(atlas_peer.config_path.stat().st_mode & 0o777, 0o600)

	@staticmethod
	@contextmanager
	def records() -> Iterator[Mock]:
		"""Return one host and the proxy VM that runs on it."""

		def get_all(doctype: str, **_arguments: object) -> list[frappe._dict]:
			return [PROXY] if doctype == "Virtual Machine" else [HOST]

		with (
			patch.object(atlas_peer_module.frappe, "get_all", side_effect=get_all) as get_all_mock,
			patch(
				"atlas.atlas.core.mesh_address.frappe.get_single", return_value=SimpleNamespace(region_id=1)
			),
			patch.object(atlas_peer_module.frappe, "conf", frappe._dict()),
		):
			yield get_all_mock

	def settings(self, **values: object) -> SimpleNamespace:
		settings = SimpleNamespace(
			**{
				"doctype": "Atlas Settings",
				"name": "Atlas Settings",
				"region_id": 1,
				"wireguard_ip_address": None,
				"wireguard_public_key": None,
				**values,
			}
		)
		settings.db_set = lambda field, value=None: settings.__dict__.update({field: value})
		settings.get_password = lambda field: self.passwords[field]
		settings.save = lambda **_: self.passwords.update(
			wireguard_private_key=settings.wireguard_private_key
		)
		return settings
