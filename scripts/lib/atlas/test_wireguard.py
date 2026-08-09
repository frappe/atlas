"""Unit tests for the host-side WireGuard tunnel plumbing (spec/19-vpn-broker.md).

Run with bare `python3 -m unittest atlas.test_wireguard` from scripts/lib: no
Frappe, no site, no host, no nft, no wg. These cover the argv construction, the
substring/handle helpers that drive apply()/remove() without touching the host,
and the TunnelConfig sidecar (de)serialization.
"""

import shlex
import unittest

from atlas import wireguard as wg
from atlas.network_env import NetworkEnv

INTERFACE = "wg-0a1b2c3d4e5"
PORT = 51820
KEY_PATH = "/var/lib/atlas/virtual-machines/uuid/tunnels/tun.key"
CLIENT_PUB = "d8tf2yH/6dvuX9h98PFMN66l5dVfTlu2hakbGH6nnUo="
CLIENT_ADDR = "fd00:a71a:5000::1"
HOST_ADDR = "fd00:a71a:5000::/127"
VM_V6 = "2001:db8::2"


def _config() -> wg.TunnelConfig:
	return wg.TunnelConfig(
		interface=INTERFACE,
		listen_port=PORT,
		private_key_path=KEY_PATH,
		client_public_key=CLIENT_PUB,
		client_address=CLIENT_ADDR,
		host_address=HOST_ADDR,
		virtual_machine_ipv6=VM_V6,
	)


class TestCommand(unittest.TestCase):
	# The builders now return a rendered command STRING; run() shlex.splits it into
	# the same argv the old list form produced. Assert that round-trip.
	def test_link_commands(self):
		self.assertEqual(
			shlex.split(wg.link_add_command(INTERFACE)), ["ip", "link", "add", INTERFACE, "type", "wireguard"]
		)
		self.assertEqual(shlex.split(wg.link_up_command(INTERFACE)), ["ip", "link", "set", INTERFACE, "up"])
		self.assertEqual(shlex.split(wg.link_del_command(INTERFACE)), ["ip", "link", "del", INTERFACE])

	def test_addr_add_uses_host_cidr(self):
		self.assertEqual(
			shlex.split(wg.addr_add_command(INTERFACE, HOST_ADDR)),
			["ip", "-6", "addr", "add", HOST_ADDR, "dev", INTERFACE],
		)

	def test_wg_set_interface_reads_key_from_path(self):
		# listen-port rendered as a string; the key comes from a file path, never
		# inline on the command line.
		self.assertEqual(
			shlex.split(wg.wg_set_interface_command(INTERFACE, PORT, KEY_PATH)),
			["wg", "set", INTERFACE, "listen-port", "51820", "private-key", KEY_PATH],
		)

	def test_wg_set_peer_scopes_allowed_ips_to_128(self):
		self.assertEqual(
			shlex.split(wg.wg_set_peer_command(INTERFACE, CLIENT_PUB, CLIENT_ADDR)),
			["wg", "set", INTERFACE, "peer", CLIENT_PUB, "allowed-ips", f"{CLIENT_ADDR}/128"],
		)

	def test_accept_rule_targets_the_vm(self):
		# `insert` (not `add`): the pair goes to the head of the forward chain, above
		# the broad per-VM accepts that would otherwise shadow the drop below it.
		self.assertEqual(
			shlex.split(wg.accept_rule_command(INTERFACE, VM_V6)),
			[
				"insert",
				"rule",
				"inet",
				"atlas",
				"forward",
				"iifname",
				INTERFACE,
				"ip6",
				"daddr",
				VM_V6,
				"accept",
			],
		)

	def test_drop_rule_is_unconditional_for_the_interface(self):
		# The transit isolation guarantee: anything else forwarded off this interface is
		# dropped. Inserted at the head so a per-VM accept for another VM cannot pre-empt it.
		self.assertEqual(
			shlex.split(wg.drop_rule_command(INTERFACE)),
			["insert", "rule", "inet", "atlas", "forward", "iifname", INTERFACE, "drop"],
		)

	def test_host_drop_rule_targets_the_input_chain(self):
		# The host-local guarantee: a packet this tunnel addresses to the host itself
		# takes the input path, which the forward drop never sees. Appended (`add`) to the
		# dedicated input chain, which holds only per-tunnel drops, so nothing shadows it.
		self.assertEqual(
			shlex.split(wg.host_drop_rule_command(INTERFACE)),
			["add", "rule", "inet", "atlas", "input", "iifname", INTERFACE, "drop"],
		)


_LISTING = f"""chain forward {{
\ttype filter hook forward priority filter; policy accept;
\tiifname "{INTERFACE}" ip6 daddr {VM_V6} accept # handle 12
\tiifname "{INTERFACE}" drop # handle 13
\tiifname "atlas-hdeadbee" ip6 daddr 2001:db8::3 accept # handle 9
}}"""

_INPUT_LISTING = f"""chain input {{
\ttype filter hook input priority filter; policy accept;
\tiifname "{INTERFACE}" drop # handle 7
}}"""


class TestRulePresenceAndHandles(unittest.TestCase):
	def test_has_accept_and_drop_match_this_interface(self):
		self.assertTrue(wg._has_accept(_LISTING, INTERFACE, VM_V6))
		self.assertTrue(wg._has_drop(_LISTING, INTERFACE))

	def test_has_accept_false_for_other_vm_or_interface(self):
		self.assertFalse(wg._has_accept(_LISTING, INTERFACE, "2001:db8::99"))
		self.assertFalse(wg._has_accept(_LISTING, "wg-absent00000", VM_V6))

	def test_handles_only_for_this_interface(self):
		# 12 and 13 are this tunnel's rules; 9 belongs to another VM's veth rule.
		self.assertEqual(list(wg._handles_for(_LISTING, INTERFACE)), ["12", "13"])
		self.assertEqual(list(wg._handles_for(_LISTING, "atlas-hdeadbee")), ["9"])

	def test_input_drop_detected_and_handle_scraped(self):
		# remove_tunnel scans the input chain too; apply skips re-adding a present drop.
		self.assertTrue(wg._has_drop(_INPUT_LISTING, INTERFACE))
		self.assertEqual(list(wg._handles_for(_INPUT_LISTING, INTERFACE)), ["7"])


class TestTunnelConfigSidecar(unittest.TestCase):
	def test_round_trips_through_env_text(self):
		config = _config()
		restored = wg.TunnelConfig.from_env(NetworkEnv.parse(config.to_env_text()))
		self.assertEqual(restored, config)

	def test_from_env_fails_loud_on_missing_key(self):
		text = _config().to_env_text().replace(f"INTERFACE={INTERFACE}\n", "")
		with self.assertRaises(SystemExit):
			wg.TunnelConfig.from_env(NetworkEnv.parse(text))


if __name__ == "__main__":
	unittest.main()
