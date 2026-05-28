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
			"atlas.atlas.providers.for_provider",
			return_value=fake_impl,
		):
			result = settings.test_connection()
		self.assertTrue(result["ok"])
		self.assertEqual(result["account_label"], "x@y.com")

	def test_test_connection_throws_without_provider(self) -> None:
		previous = frappe.db.get_single_value("Atlas Settings", "provider")
		try:
			frappe.db.set_single_value(
				"Atlas Settings", "provider", "", update_modified=False
			)
			settings = frappe.get_single("DigitalOcean Settings")
			with self.assertRaises(frappe.ValidationError):
				settings.test_connection()
		finally:
			if previous:
				frappe.db.set_single_value(
					"Atlas Settings", "provider", previous, update_modified=False
				)

	def test_credential_check_ok(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(ok=True, account_label="x@y.com")
		settings = frappe.get_single("DigitalOcean Settings")
		with patch(
			"atlas.atlas.providers.for_provider",
			return_value=fake_impl,
		):
			result = settings.credential_check()
		self.assertTrue(result["ok"])

	def test_credential_check_returns_error_dict_without_provider(self) -> None:
		previous = frappe.db.get_single_value("Atlas Settings", "provider")
		try:
			frappe.db.set_single_value(
				"Atlas Settings", "provider", "", update_modified=False
			)
			settings = frappe.get_single("DigitalOcean Settings")
			result = settings.credential_check()
			self.assertFalse(result["ok"])
			self.assertIn("provider", result["error"])
		finally:
			if previous:
				frappe.db.set_single_value(
					"Atlas Settings", "provider", previous, update_modified=False
				)
