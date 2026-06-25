import base64
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import tunnel as module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.doctype.vpn_tunnel import vpn_tunnel as controller
from atlas.atlas.networking import TUNNEL_PORT_BASE
from atlas.tests._mocks import fake_task
from atlas.tests.test_ssh_key import _ensure_atlas_user_role, _make_user

CLIENT_PUB = base64.standard_b64encode(b"\x22" * 32).decode()
SERVER_PUB = base64.standard_b64encode(b"\x33" * 32).decode()
INTRUDER = "tunnel-intruder@example.com"
OWNER = "tunnel-owner@example.com"


def _up_task():
	return fake_task(name="task-up", stdout=f'ATLAS_RESULT={{"server_public_key": "{SERVER_PUB}"}}')


def _purge() -> None:
	for name in frappe.get_all("VPN Tunnel", pluck="name"):
		frappe.delete_doc("VPN Tunnel", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestRequestTunnel(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_ensure_atlas_user_role()
		_purge()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_returns_a_ready_client_config(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=_up_task()):
			config = module.request_tunnel(vm.name, CLIENT_PUB, label="laptop")
		server_ipv4 = frappe.db.get_value("Server", vm.server, "ipv4_address")
		self.assertEqual(config["server_public_key"], SERVER_PUB)
		self.assertEqual(config["allowed_ips"], f"{vm.ipv6_address}/128")
		self.assertEqual(config["endpoint"], f"{server_ipv4}:{TUNNEL_PORT_BASE}")
		self.assertTrue(config["client_address"].endswith("/128"))
		# The config is ready to paste, with the client's own key left blank and
		# the host key + endpoint + scoped AllowedIPs filled in.
		self.assertIn("PrivateKey = <your client private key>", config["config"])
		self.assertIn(SERVER_PUB, config["config"])
		self.assertIn(config["endpoint"], config["config"])
		self.assertIn(f"{vm.ipv6_address}/128", config["config"])
		self.assertIn("wg genkey", config["instructions"])
		# The row landed Active.
		tunnel = frappe.get_doc("VPN Tunnel", config["name"])
		self.assertEqual(tunnel.status, "Active")
		self.assertEqual(tunnel.label, "laptop")

	def test_rejects_invalid_client_key(self) -> None:
		vm = _new_vm()
		with self.assertRaises(frappe.ValidationError) as raised:
			module.request_tunnel(vm.name, "not-a-key")
		self.assertIn("valid WireGuard public key", str(raised.exception))
		# Nothing was created.
		self.assertEqual(frappe.get_all("VPN Tunnel", filters={"virtual_machine": vm.name}), [])

	def test_rejects_unknown_vm(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			module.request_tunnel("00000000-0000-0000-0000-000000000000", CLIENT_PUB)
		self.assertIn("not found", str(raised.exception))

	def test_owner_may_request(self) -> None:
		vm = _new_vm()
		frappe.db.set_value("Virtual Machine", vm.name, "owner", OWNER)
		_make_user(OWNER, role="Atlas User")
		frappe.set_user(OWNER)
		with patch.object(controller, "run_task", return_value=_up_task()):
			config = module.request_tunnel(vm.name, CLIENT_PUB)
		self.assertEqual(config["server_public_key"], SERVER_PUB)

	def test_non_owner_is_denied(self) -> None:
		vm = _new_vm()  # owned by Administrator
		_make_user(INTRUDER, role="Atlas User")
		frappe.set_user(INTRUDER)
		with self.assertRaises(frappe.PermissionError):
			module.request_tunnel(vm.name, CLIENT_PUB)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.get_all("VPN Tunnel", filters={"virtual_machine": vm.name}), [])
