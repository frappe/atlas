"""Unit tests for `networkd.commands` — the wg-mesh apply / bring-up command
builders (spec §16.4 / §16.5). Command construction verified offline with
`shlex.split` against the canonical argv, the same idiom
`test_host_mesh.py` uses for the existing lib.
"""

import shlex
import unittest

from atlas.networkd import commands as c


class TestCommandBuilders(unittest.TestCase):
	def test_link_add(self):
		self.assertEqual(
			shlex.split(c.link_add_command()),
			["ip", "link", "add", "dev", "wg-mesh", "type", "wireguard"],
		)

	def test_link_mtu(self):
		self.assertEqual(
			shlex.split(c.link_mtu_command(1420)),
			["ip", "link", "set", "dev", "wg-mesh", "mtu", "1420"],
		)

	def test_link_up(self):
		self.assertEqual(shlex.split(c.link_up_command()), ["ip", "link", "set", "dev", "wg-mesh", "up"])

	def test_addr_add_is_replace(self):
		self.assertEqual(
			shlex.split(c.addr_add_command("fdaa:0:0:a1b2::1")),
			["ip", "-6", "addr", "replace", "fdaa:0:0:a1b2::1/128", "dev", "wg-mesh"],
		)

	def test_route_owns_private_plane(self):
		self.assertEqual(
			shlex.split(c.route_add_command()),
			["ip", "-6", "route", "replace", "fdaa::/16", "dev", "wg-mesh"],
		)

	def test_set_key_uses_data_dir_key(self):
		# Issue A: key path moved from the controller-pushed
		# `/etc/atlas-host-mesh.key` to the daemon's `/etc/atlas-networkd/wg-private-key`.
		argv = shlex.split(c.set_key_command(51820))
		self.assertEqual(
			argv,
			[
				"wg",
				"set",
				"wg-mesh",
				"private-key",
				"/etc/atlas-networkd/wg-private-key",
				"listen-port",
				"51820",
			],
		)

	def test_syncconf_uses_run_path(self):
		argv = shlex.split(c.syncconf_command())
		# The hot-path config lives under /run (tmpfs, never persisted) — the
		# persisted state is the JSON in /var/lib; /run/wg-mesh.conf is ephemeral.
		self.assertEqual(argv[:3], ["wg", "syncconf", "wg-mesh"])
		self.assertIn("/run/atlas-networkd/wg-mesh.conf", " ".join(argv))


class TestApplyScript(unittest.TestCase):
	def test_syncconf_before_set_key(self):
		# Load-bearing ordering (spec §16.4): syncconf FIRST, set private-key
		# LAST. The script body must list them in that order — the assertion in
		# `apply_script` enforces this at construction time.
		script = c.apply_script()
		self.assertIn("syncconf", script)
		self.assertIn("private-key", script)
		self.assertLess(script.index("syncconf"), script.index("private-key"))

	def test_set_e_aborts_on_failure(self):
		# `set -e` so a failed syncconf doesn't leave the device keyless.
		self.assertTrue(c.apply_script().startswith("set -e;"))

	def test_default_paths(self):
		# The default config path is /run (hot path), the default key path is the
		# daemon's data dir (Issue A).
		script = c.apply_script()
		self.assertIn("/run/atlas-networkd/wg-mesh.conf", script)
		self.assertIn("/etc/atlas-networkd/wg-private-key", script)


class TestBringUpScript(unittest.TestCase):
	def test_bring_up_creates_device_then_applies(self):
		s = c.bring_up_script("fdaa:0:0:1::1")
		# Device creation is guarded with `if ! ip link show`; bring up happens
		# only once. Then addr/mtu/up/route, then syncconf+key (via apply_script).
		self.assertIn("ip link add dev wg-mesh type wireguard", s)
		self.assertIn("ip -6 addr replace fdaa:0:0:1::1/128", s)
		self.assertIn("ip -6 route replace fdaa::/16 dev wg-mesh", s)
		self.assertIn("mtu 1420", s)
		self.assertIn("syncconf", s)
		self.assertIn("private-key", s)

	def test_bring_up_skips_empty_config(self):
		# A fresh host before the first render has an empty/absent config; the
		# apply is gated on `[ -s <path> ]` so a peer-empty device comes up clean
		# and waits, mirroring `bring_up_mesh`.
		s = c.bring_up_script("fdaa:0:0:1::1")
		self.assertIn("if [ -s /run/atlas-networkd/wg-mesh.conf ]", s)


if __name__ == "__main__":
	unittest.main()
