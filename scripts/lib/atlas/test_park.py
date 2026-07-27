"""Unit tests for the host-side "parked" reachability + TCP-SYN wake trap
(spec/32-sleepy-vms.md).

Run with bare `python3 -m unittest atlas.test_park` from scripts/lib: no Frappe,
no site, no host, no nft. These cover the counter-name derivation (and its
inverse, which the daemon relies on), the nft argv construction, and the exact
SYN-only match / drop verb that make the trap "TCP only".
"""

import shlex
import unittest

from atlas import park

VM_V6 = "2400:6180:100:d0:0:1:5835:d003"
UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
HEX = "3f2504e04f8941d39a0c0305e82c3301"


class TestCounterName(unittest.TestCase):
	def test_strips_dashes_and_prefixes(self):
		self.assertEqual(park.counter_name(UUID), f"wake_{HEX}")

	def test_round_trips_through_uuid(self):
		self.assertEqual(park.uuid_for_counter(park.counter_name(UUID)), UUID)

	def test_rejects_foreign_counter_names(self):
		# Not ours, wrong length, or non-hex — the daemon must ignore these so it
		# never derives a bogus uuid from another table's counter.
		self.assertIsNone(park.uuid_for_counter("bytes_total"))
		self.assertIsNone(park.uuid_for_counter("wake_short"))
		self.assertIsNone(park.uuid_for_counter("wake_" + "z" * 32))


class TestCounterCommand(unittest.TestCase):
	def test_adds_named_table_counter(self):
		self.assertEqual(
			shlex.split(park.counter_command(UUID)),
			["add", "counter", "inet", "atlas", f"wake_{HEX}"],
		)


class TestWakeRuleCommand(unittest.TestCase):
	def test_matches_bare_syn_and_drops_into_named_counter(self):
		command = park.wake_rule_command(VM_V6, UUID)
		self.assertEqual(
			shlex.split(command),
			["add", "rule", "inet", "atlas", "forward", "ip6", "daddr", VM_V6,
			 "tcp", "flags", "syn", "/", "fin,syn,rst,ack",
			 "counter", "name", f"wake_{HEX}", "drop"],
		)  # fmt: skip

	def test_is_tcp_only_and_drops_not_rejects(self):
		# "TCP only" (no ICMP/UDP wake) and drop-so-the-client-retransmits are the
		# two load-bearing properties of the trap — assert them explicitly.
		command = park.wake_rule_command(VM_V6, UUID)
		self.assertIn("tcp flags syn / fin,syn,rst,ack", command)
		self.assertTrue(command.rstrip().endswith("drop"))
		self.assertNotIn("reject", command)

	def test_quotes_the_v6_but_keeps_the_counter_name_literal(self):
		# The /128 is data (goes through a quoted hole); the counter name is a fixed
		# hex identifier and stays literal so nft parses it as an identifier.
		command = park.wake_rule_command("2001:db8::1", UUID)
		self.assertEqual(shlex.split(command).count(f"wake_{HEX}"), 1)


if __name__ == "__main__":
	unittest.main()
