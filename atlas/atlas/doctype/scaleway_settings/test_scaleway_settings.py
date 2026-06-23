"""Tests for the Scaleway Settings Single's controller methods."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import AuthResult
from atlas.tests.fixtures import make_provider


class TestScalewaySettings(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-scw-settings-prov", provider_type="Scaleway")

	def test_test_connection_resolves_scaleway(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(ok=True, account_label="my-project")
		settings = frappe.get_single("Scaleway Settings")
		with patch(
			"atlas.atlas.providers.for_provider_type",
			return_value=fake_impl,
		) as for_type:
			result = settings.test_connection()
		for_type.assert_called_once_with("Scaleway")
		fake_impl.authenticate.assert_called_once_with()
		self.assertTrue(result["ok"])
		self.assertEqual(result["account_label"], "my-project")
