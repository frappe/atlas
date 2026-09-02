# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestAtlasSettings(IntegrationTestCase):
	"""
	Integration tests for AtlasSettings.
	Use this class for testing interactions between multiple components.
	"""

	def test_on_update_builds_no_controller_during_insert(self):
		"""App install saves an empty Atlas Settings. The providers cannot be built from it."""
		settings = frappe.get_single("Atlas Settings")
		settings.flags.in_insert = True

		with (
			patch(
				"atlas.atlas.core.server_providers.get_server_provider",
				side_effect=AssertionError("server provider must not be built during insert"),
			),
			patch(
				"atlas.atlas.core.dns_providers.get_dns_provider",
				side_effect=AssertionError("dns provider must not be built during insert"),
			),
		):
			settings.on_update()
