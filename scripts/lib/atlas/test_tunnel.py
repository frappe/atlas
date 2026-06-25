"""Unit tests for the WireGuard spoke config generation.

Run with bare `python3 -m unittest atlas.test_tunnel` from scripts/lib: no Frappe, no
site, no host, no wg. Covers the spoke conf builder that drives ensure_interface()
without touching the host.
"""

import unittest

from atlas import tunnel

HUB_PUB = "9Lm66MPE0EfyG4ZytQ3TTer62BPJs09D4hVRsjzTHSA="
HUB_ENDPOINT = "203.0.113.5:51820"


class TestSpokeConf(unittest.TestCase):
	def test_interface_and_single_hub_peer(self):
		conf = tunnel.spoke_conf("PRIV=", "10.88.0.2/32", 51820, HUB_PUB, HUB_ENDPOINT, "10.88.0.0/16", 25)
		self.assertIn("[Interface]", conf)
		self.assertIn("PrivateKey = PRIV=", conf)
		self.assertIn("Address = 10.88.0.2/32", conf)
		self.assertIn("ListenPort = 51820", conf)
		self.assertIn("[Peer]", conf)
		self.assertIn(f"PublicKey = {HUB_PUB}", conf)
		self.assertIn(f"Endpoint = {HUB_ENDPOINT}", conf)
		# the hub peer routes the whole tunnel CIDR
		self.assertIn("AllowedIPs = 10.88.0.0/16", conf)
		self.assertIn("PersistentKeepalive = 25", conf)
		# exactly one peer (the hub)
		self.assertEqual(conf.count("[Peer]"), 1)

	def test_default_keepalive(self):
		conf = tunnel.spoke_conf("K", "10.88.0.2/32", 51820, HUB_PUB, HUB_ENDPOINT, "10.88.0.0/16")
		self.assertIn("PersistentKeepalive = 25", conf)


if __name__ == "__main__":
	unittest.main()
