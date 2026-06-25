"""Unit tests for the management-plane firewall ruleset generation.

Run with bare `python3 -m unittest atlas.test_firewall` from scripts/lib: no Frappe,
no site, no host, no nft. Covers the nft ruleset construction that drives apply()/
persist() without touching the host.
"""

import unittest

from atlas import mgmt_firewall as firewall


class TestMgmtRuleset(unittest.TestCase):
	def test_locks_public_iface_allows_wg_port(self):
		ruleset = firewall.mgmt_ruleset("eth0", 51820, [])
		self.assertIn("table inet atlas_mgmt {", ruleset)
		# only the public interface is sent to the drop chain…
		self.assertIn('iifname "eth0" jump public_input', ruleset)
		# …so everything else (lo, wg0, private NIC) rides policy accept
		self.assertIn("policy accept;", ruleset)
		self.assertIn("ct state established,related accept", ruleset)
		self.assertIn("udp dport 51820 accept", ruleset)
		# the public_input chain is default-deny (ends in drop)
		self.assertIn("\t\tdrop\n", ruleset)

	def test_no_allow_ports_by_default(self):
		self.assertNotIn("tcp dport", firewall.mgmt_ruleset("eth0", 51820, []))

	def test_public_allow_ports_rendered(self):
		ruleset = firewall.mgmt_ruleset("eth0", 51820, ["22", "8080"])
		self.assertIn("tcp dport { 22, 8080 } accept", ruleset)

	def test_loadable_prefixes_add_delete_idiom(self):
		ruleset = firewall.loadable_ruleset("eth0", 51820, [])
		self.assertTrue(ruleset.startswith("table inet atlas_mgmt {}\ndelete table inet atlas_mgmt\n"))
		self.assertEqual(ruleset.count("type filter hook input"), 1)


if __name__ == "__main__":
	unittest.main()
