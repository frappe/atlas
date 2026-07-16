import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)

SAT_KEY = "ssh-ed25519 AAAASATELLITEORCHESTRATORKEY satellite@orchestrator"


def _set_satellite_keys(value: str) -> None:
	frappe.db.set_single_value("Atlas Settings", "satellite_public_keys", value)


class TestGuestSatelliteKeyInjection(IntegrationTestCase):
	"""Atlas hands over a bare box; it injects the Satellite orchestrator's key into the
	guest so Satellite can SSH in and run services (spec/30)."""

	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
		self.addCleanup(_set_satellite_keys, "")

	def test_owner_key_only_when_no_satellite(self) -> None:
		_set_satellite_keys("")
		vm = _new_vm()
		self.assertEqual(vm._guest_authorized_keys(), vm.ssh_public_key.strip())

	def test_satellite_key_appended(self) -> None:
		_set_satellite_keys(SAT_KEY)
		vm = _new_vm()
		lines = vm._guest_authorized_keys().splitlines()
		self.assertIn(vm.ssh_public_key.strip(), lines)
		self.assertIn(SAT_KEY, lines)
		# It rides the provision Task env, which the rootfs writes verbatim to
		# the guest's /root/.ssh/authorized_keys.
		self.assertIn(SAT_KEY, vm._provision_variables()["SSH_PUBLIC_KEY"])
