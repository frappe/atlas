"""Unit tests for the customer-gateway bring-up command builders (spec/26, reference §9).

Run with bare `python3 -m unittest atlas.test_gateway` from scripts/lib: no Frappe, no
host, no wg, no eBPF. These cover the argv construction (link / key / tc guard / nft
drop) that bring_up_gateway() runs, without touching the host. The one host-touching
function (bring_up_gateway) + the live eBPF attach are proven on a real wg0, not here.
"""

import shlex
import unittest

from atlas import gateway as gw


class TestCommands(unittest.TestCase):
	def test_link_add(self):
		self.assertEqual(
			shlex.split(gw.link_add_command()),
			["ip", "link", "add", "dev", "wg0", "type", "wireguard"],
		)

	def test_link_mtu(self):
		self.assertEqual(
			shlex.split(gw.link_mtu_command(1420)), ["ip", "link", "set", "dev", "wg0", "mtu", "1420"]
		)

	def test_set_key_from_file_never_inline(self):
		# The key is set from a 0600 file, never on the argv (which `ps` could read).
		argv = shlex.split(gw.set_key_command(51820))
		self.assertEqual(
			argv, ["wg", "set", "wg0", "private-key", "/etc/wireguard/wg0.key", "listen-port", "51820"]
		)

	def test_genkey_writes_0600_file(self):
		# umask 077 so the minted key file is 0600; kept on the gateway, reused on re-run.
		self.assertIn("umask 077", gw.genkey_command())
		self.assertIn("/etc/wireguard/wg0.key", gw.genkey_command())

	def test_attach_guard_is_tc_ingress_direct_action(self):
		# The static same_48 guard: wg0 tc INGRESS, direct-action, section `tc` (gotcha #2).
		argv = shlex.split(gw.attach_guard_command("/x/vpc_guard.bpf.o"))
		self.assertEqual(
			argv,
			[
				"tc",
				"filter",
				"add",
				"dev",
				"wg0",
				"ingress",
				"bpf",
				"da",
				"obj",
				"/x/vpc_guard.bpf.o",
				"sec",
				"tc",
			],
		)

	def test_clsact_before_filter(self):
		self.assertEqual(shlex.split(gw.clsact_command()), ["tc", "qdisc", "add", "dev", "wg0", "clsact"])

	def test_input_drop_is_in_guest_own_table(self):
		# The host-local drop lives in the guest's OWN `inet gateway` table — NOT the
		# host's `inet atlas` table (which doesn't exist inside the guest).
		text = gw.input_drop_command()
		self.assertIn("inet gateway", text)
		self.assertIn('iifname "wg0" drop', text)

	def test_create_table_makes_input_chain_policy_accept(self):
		commands = gw.create_table_commands()
		self.assertTrue(any("add table inet gateway" in c for c in commands))
		self.assertTrue(any("hook input" in c and "policy accept" in c for c in commands))

	def test_public_key_read_from_key_file(self):
		# `wg pubkey < wg0.key` — reads the key by redirect, prints the shared public key.
		text = gw.public_key_command()
		self.assertTrue(text.startswith("wg pubkey"))
		self.assertIn("< /etc/wireguard/wg0.key", text)


if __name__ == "__main__":
	unittest.main()
