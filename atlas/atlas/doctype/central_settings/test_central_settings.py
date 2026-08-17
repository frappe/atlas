# Copyright (c) 2026, Frappe and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestCentralSettings(IntegrationTestCase):
	"""
	Integration tests for CentralSettings.
	Use this class for testing interactions between multiple components.
	"""

	def _settings(self, webhook_secret=None):
		settings = frappe.get_single("Central Settings")
		settings.url = "https://central.example"
		settings.api_key = "ak"
		settings.api_secret = "as"
		settings.webhook_secret = webhook_secret
		settings.save(ignore_permissions=True)
		return frappe.get_single("Central Settings")

	def test_client_passes_webhook_secret_when_set(self) -> None:
		settings = self._settings(webhook_secret="whs")
		self.assertEqual(settings.client().webhook_secret, "whs")

	def test_client_passes_none_when_webhook_secret_unset(self) -> None:
		# A not-yet-rotated instance: client() must not crash decrypting an unset field.
		settings = self._settings(webhook_secret=None)
		self.assertIsNone(settings.client().webhook_secret)
