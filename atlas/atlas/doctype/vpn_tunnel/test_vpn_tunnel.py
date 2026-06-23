import base64
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.doctype.vpn_tunnel import vpn_tunnel as module
from atlas.atlas.networking import TUNNEL_PORT_BASE, tunnel_overlay_link
from atlas.tests._mocks import fake_task

# A syntactically valid client public key (32 bytes, standard base64).
CLIENT_PUB = base64.standard_b64encode(b"\x22" * 32).decode()
SERVER_PUB = base64.standard_b64encode(b"\x33" * 32).decode()


def _up_task():
	return fake_task(name="task-up", stdout=f'ATLAS_RESULT={{"server_public_key": "{SERVER_PUB}"}}')


def _make_tunnel(vm: str, **overrides) -> "frappe.model.document.Document":
	doc = {"doctype": "VPN Tunnel", "virtual_machine": vm, "client_public_key": CLIENT_PUB}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _purge() -> None:
	for name in frappe.get_all("VPN Tunnel", pluck="name"):
		frappe.delete_doc("VPN Tunnel", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestVPNTunnel(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_insert_allocates_slot_and_derives_from_vm(self) -> None:
		vm = _new_vm()
		tunnel = _make_tunnel(vm.name)
		self.assertEqual(tunnel.status, "Pending")
		self.assertEqual(tunnel.transport, "public-ipv4")
		self.assertEqual(tunnel.server, vm.server)
		self.assertEqual(tunnel.slot_index, 0)
		self.assertEqual(tunnel.listen_port, TUNNEL_PORT_BASE)
		self.assertTrue(tunnel.interface_name.startswith("wg-"))
		# client_address is the client end of slot 0's /127 overlay.
		_, client_cidr = tunnel_overlay_link(0)
		self.assertEqual(tunnel.client_address, client_cidr.split("/", 1)[0])

	def test_second_tunnel_takes_next_slot(self) -> None:
		first = _make_tunnel(_new_vm().name)
		second = _make_tunnel(_new_vm().name)
		self.assertEqual((first.slot_index, second.slot_index), (0, 1))
		self.assertEqual(second.listen_port, TUNNEL_PORT_BASE + 1)

	def test_revoked_slot_is_reused(self) -> None:
		first = _make_tunnel(_new_vm().name)
		with patch.object(module, "run_task", return_value=fake_task(name="t")):
			first.revoke()
		# Slot 0 is back in the pool now the only tunnel is Revoked.
		second = _make_tunnel(_new_vm().name)
		self.assertEqual(second.slot_index, 0)

	def test_bring_up_dispatches_up_task_and_stores_pubkey(self) -> None:
		vm = _new_vm()
		tunnel = _make_tunnel(vm.name)
		with patch.object(module, "run_task", return_value=_up_task()) as run_task:
			tunnel.bring_up()
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["script"], "vm-tunnel.py")
		variables = kwargs["variables"]
		self.assertEqual(variables["ACTION"], "up")
		self.assertEqual(variables["TUNNEL_NAME"], tunnel.name)
		self.assertEqual(variables["VIRTUAL_MACHINE_NAME"], vm.name)
		self.assertEqual(variables["INTERFACE"], tunnel.interface_name)
		self.assertEqual(variables["LISTEN_PORT"], str(tunnel.listen_port))
		self.assertEqual(variables["CLIENT_PUBLIC_KEY"], CLIENT_PUB)
		self.assertEqual(variables["CLIENT_ADDRESS"], tunnel.client_address)
		self.assertEqual(variables["HOST_ADDRESS"], tunnel_overlay_link(0)[0])
		tunnel.reload()
		self.assertEqual(tunnel.status, "Active")
		self.assertEqual(tunnel.server_public_key, SERVER_PUB)

	def test_revoke_dispatches_down_task_and_marks_revoked(self) -> None:
		tunnel = _make_tunnel(_new_vm().name)
		with patch.object(module, "run_task", return_value=fake_task(name="t-down")) as run_task:
			tunnel.revoke()
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["script"], "vm-tunnel.py")
		self.assertEqual(kwargs["variables"]["ACTION"], "down")
		self.assertEqual(kwargs["variables"]["INTERFACE"], tunnel.interface_name)
		tunnel.reload()
		self.assertEqual(tunnel.status, "Revoked")

	def test_revoke_twice_throws(self) -> None:
		tunnel = _make_tunnel(_new_vm().name)
		with patch.object(module, "run_task", return_value=fake_task(name="t")):
			tunnel.revoke()
			with self.assertRaises(frappe.ValidationError) as raised:
				tunnel.revoke()
		self.assertIn("already revoked", str(raised.exception))

	def test_bring_up_on_revoked_throws(self) -> None:
		tunnel = _make_tunnel(_new_vm().name)
		with patch.object(module, "run_task", return_value=fake_task(name="t")):
			tunnel.revoke()
			with self.assertRaises(frappe.ValidationError) as raised:
				tunnel.bring_up()
		self.assertIn("revoked", str(raised.exception))

	def test_client_public_key_is_immutable(self) -> None:
		tunnel = _make_tunnel(_new_vm().name)
		tunnel.client_public_key = SERVER_PUB
		with self.assertRaises(frappe.ValidationError) as raised:
			tunnel.save(ignore_permissions=True)
		self.assertIn("client_public_key is immutable", str(raised.exception))

	def test_terminate_revokes_tunnels(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		vm = _new_vm()
		vm.status = "Running"
		vm.last_started = frappe.utils.now_datetime()
		vm.save(ignore_permissions=True)
		tunnel = _make_tunnel(vm.name)
		# terminate-vm.py (vm_module.run_task) AND the tunnel down Task
		# (module.run_task) both fire; patch both.
		with (
			patch.object(vm_module, "run_task", return_value=fake_task(name="t-term")),
			patch.object(module, "run_task", return_value=fake_task(name="t-down")),
		):
			vm.terminate()
		tunnel.reload()
		self.assertEqual(tunnel.status, "Revoked")
