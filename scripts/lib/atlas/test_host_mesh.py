"""Unit tests for the host-side wg-mesh bring-up command builders (design §3).

Run with bare `python3 -m unittest atlas.test_host_mesh` from scripts/lib: no
Frappe, no host, no wg. These cover the argv construction (link/key/addr/route)
that bring_up_mesh() runs, without touching the host. The one host-touching function
(bring_up_mesh) is proven live on a real Scaleway host, not here.
"""

import shlex
import unittest

from atlas import host_mesh as hm


class TestCommands(unittest.TestCase):
	def test_link_add(self):
		self.assertEqual(
			shlex.split(hm.link_add_command()),
			["ip", "link", "add", "dev", "wg-mesh", "type", "wireguard"],
		)

	def test_link_mtu(self):
		self.assertEqual(
			shlex.split(hm.link_mtu_command(1420)),
			["ip", "link", "set", "dev", "wg-mesh", "mtu", "1420"],
		)

	def test_set_key_after_config(self):
		# The key is set from a 0600 file, never inline — and MUST run after any config
		# load (syncconf/setconf from a keyless file clears the interface key). This
		# asserts the command shape; the ordering is enforced in bring_up_mesh().
		argv = shlex.split(hm.set_key_command(51820))
		self.assertEqual(
			argv,
			["wg", "set", "wg-mesh", "private-key", "/etc/atlas-host-mesh.key", "listen-port", "51820"],
		)

	def test_addr_add_is_replace(self):
		# replace (not add) so a re-run is idempotent — the host's own infra /128.
		self.assertEqual(
			shlex.split(hm.addr_add_command("fdaa:0:0:a1b2::1")),
			["ip", "-6", "addr", "replace", "fdaa:0:0:a1b2::1/128", "dev", "wg-mesh"],
		)

	def test_route_owns_private_plane(self):
		self.assertEqual(
			shlex.split(hm.route_add_command()),
			["ip", "-6", "route", "replace", "fdaa::/16", "dev", "wg-mesh"],
		)

	def test_addconf_merges(self):
		# addconf (merge) not setconf/syncconf (rewrite) for the boot load, so it does
		# not clear the key we set afterwards.
		self.assertEqual(
			shlex.split(hm.addconf_command()),
			["wg", "addconf", "wg-mesh", "/etc/wireguard/wg-mesh.conf"],
		)


if __name__ == "__main__":
	unittest.main()
