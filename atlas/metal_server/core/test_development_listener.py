from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.metal_server.core import development_listener
from atlas.metal_server.core.atlas_peer import AtlasPeer
from atlas.metal_server.core.development_listener import DevelopmentListener, DevelopmentListenerError

CONFIG_PATH = Path("/bench/sites/site.local/private/wireguard/atlas0.conf")


class TestDevelopmentListener(UnitTestCase):
	def test_a_lost_address_suggests_a_reinstall_that_keeps_the_listener_port_open(self) -> None:
		"""A reinstall without the listener port would close it in the atlas0 firewall."""
		with (
			patch.dict(frappe.conf, {"atlas_internal_url": "http://[fdab:0::5]:8001"}),
			patch.object(AtlasPeer, "__init__", return_value=None),
			patch.object(AtlasPeer, "config_path", new_callable=PropertyMock, return_value=CONFIG_PATH),
			patch.object(DevelopmentListener, "check"),
			patch.object(
				development_listener, "ForwardingServer", side_effect=OSError(errno.EADDRNOTAVAIL, "")
			),
			self.assertRaises(DevelopmentListenerError) as raised,
		):
			DevelopmentListener("site.local").serve()

		command = str(raised.exception).splitlines()[-1]
		self.assertIn('ATLAS_WIREGUARD_TCP_PORTS="22, 80, 443, 2222, 8001"', command)
		self.assertTrue(command.endswith(f"install-atlas-wireguard.sh {CONFIG_PATH}"))
