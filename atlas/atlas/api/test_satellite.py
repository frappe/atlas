import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import satellite as module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests.test_ssh_key import _make_user


def _purge() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestSatelliteApi(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = _ensure_test_server()
		_ensure_test_image()
		_purge()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_get_virtual_machine_payload(self) -> None:
		# The mirror a Satellite keeps: identity, tenant, and the two SSH targets.
		vm = _new_vm()
		payload = module.get_virtual_machine(vm.name)
		self.assertEqual(payload["name"], vm.name)
		self.assertEqual(payload["server"], self.server)
		self.assertEqual(payload["server_ipv4"], "10.0.0.99")  # host SSH target
		self.assertTrue(payload["guest_ipv6"].startswith("2001:db8:1::"))  # guest SSH target
		self.assertIn("status", payload)
		# Provisioner facts a Satellite needs to install a site (image bake mode + warm
		# boot), not service state.
		self.assertIn("build_mode", payload)
		self.assertEqual(payload["warm"], False)
		# The boundary is service-free: no role leaks through.
		self.assertNotIn("is_proxy", payload)
		self.assertNotIn("is_gateway", payload)

	def test_list_virtual_machines(self) -> None:
		a = _new_vm()
		b = _new_vm()
		names = {p["name"] for p in module.list_virtual_machines()}
		self.assertIn(a.name, names)
		self.assertIn(b.name, names)

	def test_get_server_payload(self) -> None:
		payload = module.get_server(self.server)
		self.assertEqual(payload["ipv4"], "10.0.0.99")
		self.assertEqual(payload["status"], "Active")

	def test_read_api_requires_system_manager(self) -> None:
		# The boundary is admin-token-authed, exactly like the Central inbound API.
		vm = _new_vm()
		frappe.set_user(_make_user("satellite-intruder@example.com", role=None))
		with self.assertRaises(frappe.PermissionError):
			module.get_virtual_machine(vm.name)
