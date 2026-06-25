from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.firewall import firewall as module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task


def _make_firewall(vm: str, rules=(), enabled: int = 1, **overrides):
	doc = {
		"doctype": "Firewall",
		"virtual_machine": vm,
		"enabled": enabled,
		"rules": [{"protocol": protocol, "port": port} for (protocol, port) in rules],
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _purge() -> None:
	# delete fires on_trash -> _clear_on_host -> run_task; mock it so teardown never
	# reaches the (real, unreachable) test server.
	with patch.object(module, "run_task", return_value=fake_task(name="fw-purge")):
		for name in frappe.get_all("Firewall", pluck="name"):
			frappe.delete_doc("Firewall", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestFirewall(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_insert_derives_from_vm_and_defaults_disabled(self) -> None:
		# Inserting alone never touches the host (no on_save dispatch) — apply is the
		# explicit sync() verb, like VPN Tunnel.bring_up.
		vm = _new_vm()
		firewall = _make_firewall(vm.name, rules=[("tcp", 443)])
		self.assertEqual(firewall.server, vm.server)
		self.assertEqual(firewall.tenant, vm.tenant)
		self.assertEqual(firewall.status, "Disabled")

	def test_sync_applies_rules(self) -> None:
		vm = _new_vm()
		firewall = _make_firewall(vm.name, rules=[("tcp", 443), ("udp", 1194)])
		with patch.object(module, "run_task", return_value=fake_task(name="fw")) as run_task:
			firewall.sync()
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["script"], "firewall-apply.py")
		self.assertEqual(kwargs["variables"]["ACTION"], "apply")
		self.assertEqual(kwargs["variables"]["VIRTUAL_MACHINE_NAME"], vm.name)
		self.assertEqual(kwargs["variables"]["RULE"], ["tcp/443", "udp/1194"])
		firewall.reload()
		self.assertEqual(firewall.status, "Active")

	def test_sync_empty_rules_is_deny_all_public(self) -> None:
		vm = _new_vm()
		firewall = _make_firewall(vm.name, rules=[])
		with patch.object(module, "run_task", return_value=fake_task(name="fw")) as run_task:
			firewall.sync()
			(_, kwargs) = run_task.call_args
		# Applied with an empty RULE list -> deny all public (VPN-only).
		self.assertEqual(kwargs["variables"]["ACTION"], "apply")
		self.assertEqual(kwargs["variables"]["RULE"], [])

	def test_sync_disabled_clears(self) -> None:
		vm = _new_vm()
		firewall = _make_firewall(vm.name, rules=[("tcp", 443)], enabled=0)
		with patch.object(module, "run_task", return_value=fake_task(name="fw")) as run_task:
			firewall.sync()
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["variables"]["ACTION"], "clear")
		firewall.reload()
		self.assertEqual(firewall.status, "Disabled")

	def test_one_firewall_per_vm(self) -> None:
		vm = _new_vm()
		_make_firewall(vm.name, rules=[("tcp", 443)])
		with self.assertRaises(frappe.ValidationError) as raised:
			_make_firewall(vm.name, rules=[("tcp", 80)])
		self.assertIn("already exists", str(raised.exception))

	def test_duplicate_rule_throws(self) -> None:
		vm = _new_vm()
		with self.assertRaises(frappe.ValidationError) as raised:
			_make_firewall(vm.name, rules=[("tcp", 443), ("tcp", 443)])
		self.assertIn("Duplicate", str(raised.exception))

	def test_out_of_range_port_throws(self) -> None:
		vm = _new_vm()
		with self.assertRaises(frappe.ValidationError) as raised:
			_make_firewall(vm.name, rules=[("tcp", 70000)])
		self.assertIn("out of range", str(raised.exception))

	def test_virtual_machine_is_immutable(self) -> None:
		vm = _new_vm()
		other = _new_vm()
		firewall = _make_firewall(vm.name, rules=[("tcp", 443)])
		firewall.virtual_machine = other.name
		with self.assertRaises(frappe.ValidationError) as raised:
			firewall.save(ignore_permissions=True)
		self.assertIn("immutable", str(raised.exception))

	def test_trash_clears_on_host(self) -> None:
		vm = _new_vm()
		firewall = _make_firewall(vm.name, rules=[("tcp", 443)])
		with patch.object(module, "run_task", return_value=fake_task(name="fw")) as run_task:
			firewall.delete(ignore_permissions=True)
			(_, kwargs) = run_task.call_args
		self.assertEqual(kwargs["variables"]["ACTION"], "clear")

	def test_sync_skips_terminated_vm(self) -> None:
		vm = _new_vm()
		firewall = _make_firewall(vm.name, rules=[("tcp", 443)])
		frappe.db.set_value("Virtual Machine", vm.name, "status", "Terminated")
		with patch.object(module, "run_task", return_value=fake_task(name="fw")) as run_task:
			firewall.sync()
		firewall.reload()
		self.assertEqual(firewall.status, "Disabled")
		run_task.assert_not_called()
