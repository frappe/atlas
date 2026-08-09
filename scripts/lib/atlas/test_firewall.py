"""Unit tests for the host-side public-ingress firewall (spec/20-firewall.md).

Run with bare `python3 -m unittest atlas.test_firewall` from scripts/lib: no
Frappe, no site, no host, no nft. These cover the rule parsing, the argv
construction, the handle scrape that drives apply()/remove(), and the
FirewallConfig sidecar (de)serialization.
"""

import shlex
import unittest

from atlas import firewall as fw
from atlas.network_env import NetworkEnv

UPLINK = "eth0"
VM_V6 = "2400:6180:100:d0:0:1:5835:d003"


class TestRuleParsing(unittest.TestCase):
	def test_parses_proto_and_port(self):
		self.assertEqual(fw.Rule.parse("tcp/443"), fw.Rule("tcp", 443))
		self.assertEqual(fw.Rule.parse("udp/1194"), fw.Rule("udp", 1194))

	def test_token_round_trips(self):
		self.assertEqual(fw.Rule.parse("tcp/22").token(), "tcp/22")

	def test_rejects_bad_protocol(self):
		with self.assertRaises(SystemExit):
			fw.Rule.parse("icmp/0")

	def test_rejects_missing_separator(self):
		with self.assertRaises(SystemExit):
			fw.Rule.parse("443")

	def test_rejects_non_integer_port(self):
		with self.assertRaises(SystemExit):
			fw.Rule.parse("tcp/https")

	def test_rejects_out_of_range_port(self):
		with self.assertRaises(SystemExit):
			fw.Rule.parse("tcp/70000")
		with self.assertRaises(SystemExit):
			fw.Rule.parse("tcp/0")


class TestRuleCommand(unittest.TestCase):
	# The helpers now return a rendered command STRING (split into argv by run()),
	# so we assert the rendered text. A `shlex.split` of each must yield the argv the
	# previous list form produced — that round-trip is what guarantees behaviour is
	# unchanged.
	def test_established_rule_precedes_with_conntrack(self):
		command = fw.established_rule_command(UPLINK, VM_V6)
		self.assertEqual(
			shlex.split(command),
			["add", "rule", "inet", "atlas", "public_filter", "iifname", UPLINK,
			 "ip6", "daddr", VM_V6, "ct", "state", "established,related", "accept"],
		)  # fmt: skip

	def test_port_rule_targets_proto_and_port(self):
		command = fw.port_rule_command(UPLINK, VM_V6, fw.Rule("tcp", 443))
		self.assertEqual(
			shlex.split(command),
			["add", "rule", "inet", "atlas", "public_filter", "iifname", UPLINK,
			 "ip6", "daddr", VM_V6, "tcp", "dport", "443", "accept"],
		)  # fmt: skip

	def test_drop_rule_closes_the_block(self):
		command = fw.drop_rule_command(UPLINK, VM_V6)
		self.assertEqual(
			shlex.split(command),
			["add", "rule", "inet", "atlas", "public_filter", "iifname", UPLINK,
			 "ip6", "daddr", VM_V6, "drop"],
		)  # fmt: skip

	def test_chain_runs_before_forward(self):
		# priority filter - 5 is lower than forward's filter (0), so it is evaluated
		# first and its drop pre-empts the broad per-VM accept in forward.
		command = fw.ensure_chain_command()
		self.assertIn("priority filter - 5", command)
		self.assertIn("public_filter", command)
		# The whole brace clause must remain ONE argv token (Trap 2), not be
		# re-tokenized by run()'s shlex.split.
		self.assertEqual(
			shlex.split(command)[-1], "{ type filter hook forward priority filter - 5; policy accept; }"
		)


_LISTING = f"""chain public_filter {{
\ttype filter hook forward priority filter - 5; policy accept;
\tiifname "eth0" ip6 daddr {VM_V6} ct state established,related accept # handle 20
\tiifname "eth0" ip6 daddr {VM_V6} tcp dport 443 accept # handle 21
\tiifname "eth0" ip6 daddr {VM_V6} drop # handle 22
\tiifname "eth0" ip6 daddr 2400:6180:100:d0:0:1:5835:d004 drop # handle 30
}}"""


class TestHandleScrape(unittest.TestCase):
	def test_handles_only_for_this_vm(self):
		self.assertEqual(list(fw._handles_for(_LISTING, VM_V6)), ["20", "21", "22"])

	def test_handles_skip_other_vm(self):
		self.assertEqual(list(fw._handles_for(_LISTING, "2400:6180:100:d0:0:1:5835:d004")), ["30"])


class TestFirewallConfigSidecar(unittest.TestCase):
	def test_round_trips_through_env_text(self):
		config = fw.FirewallConfig(VM_V6, (fw.Rule("tcp", 443), fw.Rule("udp", 1194)))
		restored = fw.FirewallConfig.from_env(NetworkEnv.parse(config.to_env_text()))
		self.assertEqual(restored, config)

	def test_empty_rules_is_deny_all(self):
		config = fw.FirewallConfig(VM_V6, ())
		restored = fw.FirewallConfig.from_env(NetworkEnv.parse(config.to_env_text()))
		self.assertEqual(restored.rules, ())
		self.assertEqual(restored.virtual_machine_ipv6, VM_V6)

	def test_from_env_fails_loud_on_missing_address(self):
		with self.assertRaises(SystemExit):
			fw.FirewallConfig.from_env(NetworkEnv.parse("RULES=tcp/443\n"))


if __name__ == "__main__":
	unittest.main()
