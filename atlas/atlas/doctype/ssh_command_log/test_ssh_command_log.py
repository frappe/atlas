"""Controller coverage for SSH Command Log immutability."""

import frappe
from frappe.tests import IntegrationTestCase


class TestSSHCommandLogImmutability(IntegrationTestCase):
	def _log(self):
		return frappe.get_doc(
			{
				"doctype": "SSH Command Log",
				"command": "uname -a",
				"status": "Running",
				"target_count": 1,
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

	def test_command_frozen_after_insert(self) -> None:
		log = self._log()
		log.command = "rm -rf /"
		with self.assertRaises(frappe.exceptions.ValidationError):
			log.save(ignore_permissions=True)

	def test_target_count_frozen_after_insert(self) -> None:
		log = self._log()
		log.target_count = 99
		with self.assertRaises(frappe.exceptions.ValidationError):
			log.save(ignore_permissions=True)

	def test_run_state_still_writable(self) -> None:
		log = self._log()
		log.status = "Success"
		log.ended = frappe.utils.now_datetime()
		log.save(ignore_permissions=True)
		log.reload()
		self.assertEqual(log.status, "Success")
