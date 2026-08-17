# Copyright (c) 2026, Frappe and Contributors
# See license.txt

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestCentralEventLog(IntegrationTestCase):
	def _log(self, **overrides):
		# MyISAM: not rolled back by the test teardown, so clean up explicitly.
		doc = frappe.get_doc(
			{
				"doctype": "Central Event Log",
				"event_type": "vm.created",
				"payload": json.dumps({"name": "vm-1"}),
				"status": "error",
				"attempts": 5,
				"last_error": "old failure",
				"occurred_at": "2026-07-06 00:23:05",
				**overrides,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "Central Event Log", doc.name, force=True, ignore_permissions=True
		)
		return doc

	def test_retry_delivery_redelivers_and_resets_the_budget(self):
		log = self._log()
		with patch("atlas.atlas.central_report.deliver") as deliver:
			log.retry_delivery()
		deliver.assert_called_once_with(log.name, "vm.created", {"name": "vm-1"}, "2026-07-06 00:23:05")
		log.reload()
		self.assertEqual(log.attempts, 0)
		self.assertIsNone(log.last_error)

	def test_retry_delivery_refuses_an_already_delivered_row(self):
		log = self._log(status="ok")
		with patch("atlas.atlas.central_report.deliver") as deliver:
			with self.assertRaises(frappe.ValidationError):
				log.retry_delivery()
		deliver.assert_not_called()
