from __future__ import annotations

import frappe
from frappe.tests import UnitTestCase

import atlas.service.core.wg_gateway.peers as peers
from atlas.service.core.wg_gateway.peers import render_nft, render_wireguard_conf


def peer(tenant_id: int, client_id: int, key: str, fdac: str) -> dict:
	return {
		"name": f"wg-peer-{client_id}",
		"tenant_id": tenant_id,
		"client_id": client_id,
		"public_key": key,
		"fdac_address": fdac,
	}


KEY_A = "A" * 43 + "="
KEY_B = "B" * 43 + "="


class TestWireGuardConf(UnitTestCase):
	def test_each_peer_becomes_a_setconf_peer(self) -> None:
		conf = render_wireguard_conf(
			[
				peer(42, 7, KEY_A, "fdac:1:0:2a::7"),
				peer(43, 8, KEY_B, "fdac:1:0:2b::8"),
			]
		)
		self.assertIn("PublicKey = " + KEY_A, conf)
		self.assertIn("AllowedIPs = fdac:1:0:2a::7/128", conf)
		self.assertIn("PublicKey = " + KEY_B, conf)
		self.assertIn("AllowedIPs = fdac:1:0:2b::8/128", conf)

	def test_an_empty_peer_list_keeps_a_valid_conf(self) -> None:
		self.assertTrue(render_wireguard_conf([]).startswith("# Managed by Atlas"))


class TestNft(UnitTestCase):
	def test_each_tenant_gets_one_rule_for_its_prefix(self) -> None:
		nft = render_nft(
			[
				peer(42, 7, KEY_A, "fdac:1:0:2a::7"),
				peer(42, 9, KEY_B, "fdac:1:0:2a::9"),
				peer(43, 8, KEY_A, "fdac:1:0:2b::8"),
			],
			1,
			"fdaa:1::99",
		)
		self.assertIn("ip6 saddr { fdac:1:0:2a::7, fdac:1:0:2a::9 } ip6 daddr fdaa:1:0:2a::/64", nft)
		self.assertIn("ip6 saddr { fdac:1:0:2b::8 } ip6 daddr fdaa:1:0:2b::/64", nft)
		self.assertIn("counter snat to fdaa:1::99", nft)
		self.assertIn("ct state established,related counter accept", nft)

	def test_an_empty_peer_list_drops_new_client_traffic(self) -> None:
		nft = render_nft([], 1, "fdaa:1::99")
		self.assertIn("policy drop", nft)
		self.assertNotIn("ct state new", nft)


class TestPublicKey(UnitTestCase):
	def test_a_wireguard_key_is_accepted(self) -> None:
		self.assertEqual(peers.validate_public_key(KEY_A), KEY_A)

	def test_a_short_key_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "public key"):
			peers.validate_public_key("short")

	def test_a_non_key_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "public key"):
			peers.validate_public_key(None)


class TestSyncCommand(UnitTestCase):
	def test_the_command_carries_the_interface_and_reads_the_key_on_the_vm(self) -> None:
		command = peers.SYNC_COMMAND_TEMPLATE.substitute(
			state_dir=peers.STATE_DIR,
			privatekey_path=peers.PRIVATE_KEY_PATH,
			listen_port=51820,
			peers_temporary=f"{peers.PEERS_CONF_PATH}.tmp",
			peers_content=render_wireguard_conf([peer(42, 7, KEY_A, "fdac:1:0:2a::7")]),
			peers_path=peers.PEERS_CONF_PATH,
			nft_temporary=f"{peers.NFT_PATH}.tmp",
			nft_content=render_nft([], 1, "fdaa:1::99"),
			nft_path=peers.NFT_PATH,
		)

		self.assertIn("IFS= read -r private_key < /opt/atlas/wg-gateway/privatekey", command)
		self.assertIn('echo "PrivateKey = $private_key"', command)
		self.assertIn('echo "ListenPort = 51820"', command)
		self.assertIn("[Interface]", command)
		self.assertIn(f"wg setconf wg0 {peers.PEERS_CONF_PATH}", command)
		self.assertNotIn("$listen_port", command)
		self.assertNotIn("$peers_content", command)
