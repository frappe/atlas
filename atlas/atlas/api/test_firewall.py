from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import firewall as module
from atlas.atlas.doctype.firewall import firewall as controller
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task
from atlas.tests.test_ssh_key import _ensure_atlas_user_role, _make_user

INTRUDER = "firewall-intruder@example.com"
OWNER = "firewall-owner@example.com"


def _purge() -> None:
	with patch.object(controller, "run_task", return_value=fake_task(name="fw")):
		for name in frappe.get_all("Firewall", pluck="name"):
			frappe.delete_doc("Firewall", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestFirewallApi(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_ensure_atlas_user_role()
		_purge()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_sets_and_applies(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")) as run_task:
			state = module.set_firewall(vm.name, rules=[{"protocol": "tcp", "port": 443}], label="web")
			(_, kwargs) = run_task.call_args
		self.assertEqual(state["status"], "Active")
		self.assertEqual(state["rules"], ["tcp/443"])
		self.assertEqual(kwargs["variables"]["ACTION"], "apply")
		self.assertEqual(kwargs["variables"]["RULE"], ["tcp/443"])
		row = frappe.get_doc("Firewall", state["name"])
		self.assertEqual(row.label, "web")

	def test_accepts_token_rules(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")):
			state = module.set_firewall(vm.name, rules=["tcp/443", "udp/1194"])
		self.assertEqual(state["rules"], ["tcp/443", "udp/1194"])

	def test_empty_rules_is_deny_all_public(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")) as run_task:
			state = module.set_firewall(vm.name, rules=[])
			(_, kwargs) = run_task.call_args
		self.assertEqual(state["status"], "Active")
		self.assertEqual(state["rules"], [])
		self.assertEqual(kwargs["variables"]["RULE"], [])

	def test_disabled_clears(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")) as run_task:
			state = module.set_firewall(vm.name, rules=["tcp/443"], enabled=False)
			(_, kwargs) = run_task.call_args
		self.assertEqual(state["status"], "Disabled")
		self.assertEqual(kwargs["variables"]["ACTION"], "clear")

	def test_update_reuses_the_one_row(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")):
			first = module.set_firewall(vm.name, rules=["tcp/443"])
			second = module.set_firewall(vm.name, rules=["tcp/80", "tcp/443"])
		self.assertEqual(first["name"], second["name"])
		self.assertEqual(second["rules"], ["tcp/80", "tcp/443"])
		names = frappe.get_all("Firewall", filters={"virtual_machine": vm.name}, pluck="name")
		self.assertEqual(len(names), 1)

	def test_get_firewall_none_when_absent(self) -> None:
		vm = _new_vm()
		self.assertIsNone(module.get_firewall(vm.name))

	def test_remove_firewall_deletes_and_clears(self) -> None:
		vm = _new_vm()
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")) as run_task:
			module.set_firewall(vm.name, rules=["tcp/443"])
			run_task.reset_mock()
			result = module.remove_firewall(vm.name)
			(_, kwargs) = run_task.call_args
		self.assertEqual(result["status"], "Disabled")
		self.assertEqual(kwargs["variables"]["ACTION"], "clear")
		self.assertEqual(frappe.get_all("Firewall", filters={"virtual_machine": vm.name}), [])

	def test_rejects_unknown_vm(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			module.set_firewall("00000000-0000-0000-0000-000000000000", rules=["tcp/443"])
		self.assertIn("not found", str(raised.exception))

	def test_owner_may_set(self) -> None:
		vm = _new_vm()
		frappe.db.set_value("Virtual Machine", vm.name, "owner", OWNER)
		_make_user(OWNER, role="Atlas User")
		frappe.set_user(OWNER)
		with patch.object(controller, "run_task", return_value=fake_task(name="fw")):
			state = module.set_firewall(vm.name, rules=["tcp/443"])
		self.assertEqual(state["status"], "Active")

	def test_non_owner_is_denied(self) -> None:
		vm = _new_vm()  # owned by Administrator
		_make_user(INTRUDER, role="Atlas User")
		frappe.set_user(INTRUDER)
		with self.assertRaises(frappe.PermissionError):
			module.set_firewall(vm.name, rules=["tcp/443"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.get_all("Firewall", filters={"virtual_machine": vm.name}), [])
