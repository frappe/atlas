"""Unit tests for the host-side "parked" reachability + TCP-SYN wake trap
(spec/32-sleepy-vms.md).

Run with bare `python3 -m unittest atlas.test_park` from scripts/lib: no Frappe,
no site, no host, no nft. These cover the counter-name derivation (and its
inverse, which the daemon relies on), the nft argv construction, and the exact
SYN-only match / drop verb that make the trap "TCP only".
"""

import shlex
import unittest
from typing import ClassVar

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


class _FakeHost:
	"""Records every `run`/`run_ok` the park module issues, and answers the two
	queries it branches on: whether the dummy exists and what the forward chain
	currently holds. Lets the host-mutating paths be asserted with no root, no
	nft and no network — the same bare-unittest contract as the builders above.
	"""

	def __init__(self, *, device_exists=True, counter_exists=False, chain_text="", listing=""):
		self.device_exists = device_exists
		self.counter_exists = counter_exists
		self.chain_text = chain_text
		self.listing = listing
		self.commands: list[str] = []

	def run(self, template, *args, **kwargs):
		self.commands.append(template.format(*args) if "{}" in template else template)
		if template.startswith("sudo nft -a list chain"):
			return self.listing
		if template.startswith("sudo nft list chain"):
			return self.chain_text
		return ""

	def run_ok(self, template, *args, **kwargs):
		self.commands.append(template.format(*args) if "{}" in template else template)
		if template.startswith("ip link show"):
			return self.device_exists
		if template.startswith("sudo nft list counter"):
			return self.counter_exists
		return True

	def issued(self, fragment: str) -> bool:
		return any(fragment in command for command in self.commands)


class _ParkHarness(unittest.TestCase):
	"""Swaps park's host-touching collaborators for the recorder."""

	env: ClassVar[dict] = {"VIRTUAL_MACHINE_IPV6": VM_V6}

	def install(self, host: _FakeHost, env=None) -> None:
		self.host = host
		env = self.env if env is None else env
		patches = {
			"run": host.run,
			"run_ok": host.run_ok,
			"read_network_env_optional": lambda _path: env,
			"default_route_device": lambda *a, **k: "eth0",
		}
		for name, replacement in patches.items():
			original = getattr(park, name)
			setattr(park, name, replacement)
			self.addCleanup(setattr, park, name, original)


class TestPark(_ParkHarness):
	def test_no_network_env_is_a_noop(self):
		# A VM that was never provisioned (or is already terminated) has no /128 to
		# park; touching the host for it would install a trap pointing nowhere.
		self.install(_FakeHost(), env={})
		park.park(UUID)
		self.assertEqual(self.host.commands, [])

	def test_installs_ndp_route_counter_and_rule(self):
		self.install(_FakeHost())
		park.park(UUID)
		self.assertTrue(self.host.issued(f"ip -6 neigh replace proxy {VM_V6} dev eth0"))
		self.assertTrue(self.host.issued(f"ip -6 route replace {VM_V6}/128 dev {park.PARK_DEVICE}"))
		self.assertTrue(self.host.issued(f"add counter inet atlas wake_{HEX}"))
		self.assertTrue(self.host.issued(f"counter name wake_{HEX} drop"))

	def test_creates_the_dummy_when_missing(self):
		self.install(_FakeHost(device_exists=False))
		park.park(UUID)
		self.assertTrue(self.host.issued(f"ip link add {park.PARK_DEVICE} type dummy"))

	def test_re_park_does_not_duplicate_the_rule_or_counter(self):
		# The daemon re-parks every sleeping VM at boot. A second rule would split
		# the packet count across two entries and the trap could miss the first SYN.
		existing = f"ip6 daddr {VM_V6} tcp flags syn / fin,syn,rst,ack counter name wake_{HEX} drop"
		self.install(_FakeHost(counter_exists=True, chain_text=existing))
		park.park(UUID)
		self.assertFalse(self.host.issued("add rule inet atlas forward"))
		self.assertFalse(self.host.issued("add counter inet atlas"))
		# Reachability is still re-asserted — that is the point of the boot re-sweep.
		self.assertTrue(self.host.issued(f"ip -6 route replace {VM_V6}/128"))


class TestEnsureForwardChain(_ParkHarness):
	def test_creates_table_and_chain_when_absent(self):
		# The post-reboot case: every VM on the host is sleeping, so no unit ever
		# runs vm-network-up and nothing has rebuilt the nft scaffold.
		host = _FakeHost()
		host.run_ok = lambda template, *a, **k: (  # nothing exists yet
			host.commands.append(template.format(*a)) or False
		)
		self.install(host)
		park.ensure_forward_chain()
		self.assertTrue(host.issued("add table inet atlas"))
		self.assertTrue(host.issued("add chain inet atlas forward"))

	def test_is_a_noop_when_the_scaffold_exists(self):
		self.install(_FakeHost())  # run_ok defaults to True for nft queries
		park.ensure_forward_chain()
		self.assertFalse(self.host.issued("add table"))
		self.assertFalse(self.host.issued("add chain"))

	def test_park_scaffolds_before_adding_the_counter(self):
		# Ordering matters: `nft add counter inet atlas ...` fails outright if the
		# table does not exist yet.
		self.install(_FakeHost())
		park.park(UUID)
		listed = [i for i, c in enumerate(self.host.commands) if "list table inet atlas" in c]
		added = [i for i, c in enumerate(self.host.commands) if "add counter inet atlas" in c]
		self.assertTrue(listed and added and listed[0] < added[0])


class TestUnpark(_ParkHarness):
	def test_deletes_the_rule_before_the_counter(self):
		# nft refuses to drop a counter a rule still references, so the order is
		# load-bearing, not cosmetic.
		listing = (
			f"\tip6 daddr {VM_V6} tcp flags syn / fin,syn,rst,ack counter name wake_{HEX} drop # handle 42\n"
		)
		self.install(_FakeHost(listing=listing))
		park.unpark(UUID)
		delete_rule = next(i for i, c in enumerate(self.host.commands) if "delete rule" in c)
		delete_counter = next(i for i, c in enumerate(self.host.commands) if "delete counter" in c)
		self.assertLess(delete_rule, delete_counter)
		self.assertTrue(self.host.issued("delete rule inet atlas forward handle 42"))

	def test_removes_the_parked_route(self):
		self.install(_FakeHost())
		park.unpark(UUID)
		self.assertTrue(self.host.issued(f"ip -6 route del {VM_V6}/128 dev {park.PARK_DEVICE}"))

	def test_never_parked_vm_deletes_no_rule(self):
		# An ordinary start calls unpark() too; with nothing parked it must not try
		# to delete a rule it never installed.
		self.install(_FakeHost(listing=""))
		park.unpark(UUID)
		self.assertFalse(self.host.issued("delete rule"))

	def test_leaves_proxy_ndp_alone(self):
		# vm-network-up re-`replace`s NDP and vm-network-down deletes it; unpark
		# touching it would strip reachability from a VM that is coming back up.
		self.install(_FakeHost())
		park.unpark(UUID)
		self.assertFalse(self.host.issued("neigh"))


if __name__ == "__main__":
	unittest.main()
