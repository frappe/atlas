"""Tests for the DigitalOcean Settings Single's controller methods."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import AuthResult
from atlas.tests.fixtures import make_provider


class TestDigitalOceanSettings(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-do-settings-prov")

	def test_test_connection_ok(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(
			ok=True, account_label="x@y.com", rate_limit=5000, rate_remaining=4998
		)
		settings = frappe.get_single("DigitalOcean Settings")
		with patch(
			"atlas.atlas.providers.for_provider_type",
			return_value=fake_impl,
		):
			result = settings.test_connection()
		self.assertTrue(result["ok"])
		self.assertEqual(result["account_label"], "x@y.com")
