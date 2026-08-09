"""Unit tests for the reserved-IP 1:1-NAT rule generation (anchor-based).

Run with bare `python3 -m unittest atlas.test_reserved_ip_nat` from scripts/lib:
no Frappe, no site, no host, no nft, no DO metadata. These cover the argv
construction and the substring-fingerprint / handle-scrape helpers that drive
apply()/remove() without touching the host.

The rules match the droplet's ANCHOR IP (the destination DO actually delivers a
reserved-IP packet to), not the reserved IP — proven on a live droplet; see the
module docstring and the atlas-reserved-ip-anchor-dnat finding.
"""

import shlex
import unittest

from atlas import reserved_ip_nat as nat

ANCHOR = "10.47.0.10"
GUEST = "100.64.0.10"
VETH = "atlas-h0a1b2c3"


class TestRuleCommand(unittest.TestCase):
	# The builders now return a rendered command STRING; run() shlex.splits it into
	# the same argv the old list form produced. Assert that round-trip.
	def test_prerouting_chain_is_dstnat_hook(self):
		argv = shlex.split(nat.prerouting_chain_command())
		self.assertEqual(argv[:5], ["add", "chain", "inet", "atlas", "prerouting"])
		# The brace clause must stay ONE argv token (Trap 2).
		self.assertEqual(argv[-1], "{ type nat hook prerouting priority dstnat; policy accept; }")

	def test_dnat_matches_anchor_not_reserved(self):
		# prerouting: ip daddr <ANCHOR> dnat to <guest>. Matching the anchor is the
		# whole fix — the reserved IP never appears on the droplet.
		self.assertEqual(
			shlex.split(nat.dnat_rule_command(ANCHOR, GUEST)),
			["add", "rule", "inet", "atlas", "prerouting", "ip", "daddr", ANCHOR, "dnat", "to", GUEST],
		)

	def test_snat_sources_anchor_and_inserts_at_head(self):
		# `insert` (not `add`) so the per-guest SNAT beats the /16 masquerade, and
		# the source is the ANCHOR (DO maps anchor->reserved at its edge), not the
		# reserved IP directly.
		argv = shlex.split(nat.snat_rule_command(ANCHOR, GUEST))
		self.assertEqual(argv[0], "insert")
		self.assertEqual(
			argv,
			["insert", "rule", "inet", "atlas", "postrouting", "ip", "saddr", GUEST, "snat", "to", ANCHOR],
		)

	def test_forward_accepts_inbound_toward_guest(self):
		self.assertEqual(
			shlex.split(nat.forward_rule_command(GUEST, VETH)),
			["add", "rule", "inet", "atlas", "forward", "ip", "daddr", GUEST, "oifname", VETH, "accept"],
		)


class TestRoutedReservedIp(unittest.TestCase):
	"""A routed flexible IP (Self-Managed / Scaleway Elastic Metal) arrives at the
	host destined to the reserved IP itself — no anchor. The same rule builders are
	reused with the reserved IP where the DO path passes the anchor: DNAT the
	reserved IP in, SNAT the guest out as the reserved IP, no egress policy route."""

	RESERVED = "62.210.142.186"

	def test_routed_dnat_matches_the_reserved_ip_itself(self):
		self.assertEqual(
			shlex.split(nat.dnat_rule_command(self.RESERVED, GUEST)),
			["add", "rule", "inet", "atlas", "prerouting", "ip", "daddr", self.RESERVED, "dnat", "to", GUEST],
		)

	def test_routed_snat_sources_the_reserved_ip(self):
		self.assertEqual(
			shlex.split(nat.snat_rule_command(self.RESERVED, GUEST)),
			[
				"insert",
				"rule",
				"inet",
				"atlas",
				"postrouting",
				"ip",
				"saddr",
				GUEST,
				"snat",
				"to",
				self.RESERVED,
			],
		)

	def test_routed_fingerprints_key_on_reserved_ip(self):
		dnat = f"\t\tip daddr {self.RESERVED} dnat to {GUEST}\n"
		snat = f"\t\tip saddr {GUEST} snat to {self.RESERVED}\n"
		self.assertTrue(nat._has_dnat(dnat, self.RESERVED, GUEST))
		self.assertTrue(nat._has_snat(snat, self.RESERVED, GUEST))


class TestAnchorDataclass(unittest.TestCase):
	def test_anchor_holds_address_and_gateway(self):
		a = nat.Anchor(address=ANCHOR, gateway="10.47.0.1")
		self.assertEqual(a.address, ANCHOR)
		self.assertEqual(a.gateway, "10.47.0.1")


class TestIdempotencyFingerprints(unittest.TestCase):
	def test_has_dnat_true_only_when_anchor_and_guest_and_dnat(self):
		listing = f"\t\tip daddr {ANCHOR} dnat to {GUEST}\n"
		self.assertTrue(nat._has_dnat(listing, ANCHOR, GUEST))
		# A line for a different guest must not match this guest.
		other = "\t\tip daddr 10.47.0.10 dnat to 100.64.0.14\n"
		self.assertFalse(nat._has_dnat(other, ANCHOR, GUEST))
		# A forward-only line is not a DNAT.
		self.assertFalse(nat._has_dnat("ip daddr 100.64.0.10 oifname x accept", ANCHOR, GUEST))

	def test_has_snat_true_only_with_snat_keyword(self):
		listing = f"\t\tip saddr {GUEST} snat to {ANCHOR}\n"
		self.assertTrue(nat._has_snat(listing, ANCHOR, GUEST))
		# The masquerade rule for the whole supernet is NOT this guest's SNAT.
		masq = "\t\tip saddr 100.64.0.0/16 oifname eth0 masquerade\n"
		self.assertFalse(nat._has_snat(masq, ANCHOR, GUEST))

	def test_has_forward_matches_guest_and_veth(self):
		listing = f"\t\tip daddr {GUEST} oifname {VETH} accept\n"
		self.assertTrue(nat._has_forward(listing, GUEST, VETH))
		self.assertFalse(nat._has_forward(listing, GUEST, "atlas-hdeadbee"))

	def test_handles_for_scrapes_guest_lines_only(self):
		# `nft -a` appends `# handle N`; the handle is the last token. Only lines
		# mentioning THIS guest's v4 are collected (the common key across rules).
		listing = (
			f"\t\tip daddr {ANCHOR} dnat to {GUEST} # handle 12\n"
			f"\t\tip saddr {GUEST} snat to {ANCHOR} # handle 13\n"
			"\t\tip saddr 100.64.0.0/16 oifname eth0 masquerade # handle 4\n"  # shared, not ours
			"\t\tip daddr 100.64.0.99 oifname y accept # handle 99\n"  # other guest
		)
		self.assertEqual(list(nat._handles_for(listing, GUEST)), ["12", "13"])

	def test_handles_for_empty_on_missing_chain(self):
		# remove() lists with check=False; a missing chain yields "" -> no handles.
		self.assertEqual(list(nat._handles_for("", GUEST)), [])


if __name__ == "__main__":
	unittest.main()
