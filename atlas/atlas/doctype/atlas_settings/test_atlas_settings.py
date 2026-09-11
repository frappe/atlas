# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, now_datetime

from atlas.atlas.core.tls.certificate import create_private_key, serialize_private_key
from atlas.atlas.core.tls.test_certificate import self_signed_certificate

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class TestProxyClusterPassword(UnitTestCase):
	def test_rotation_action_checks_the_system_manager_role(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		settings = MagicMock()
		with (
			patch("frappe.only_for") as only_for,
			patch("frappe.msgprint"),
		):
			AtlasSettings.rotate_proxy_cluster_password(settings)

		only_for.assert_called_once_with("System Manager")
		settings._rotate_proxy_cluster_password.assert_called_once()

	def test_rotation_retains_one_previous_password(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		settings = MagicMock()
		settings.get_password.return_value = "current-password"
		with patch("frappe.generate_hash", return_value="new-password"):
			AtlasSettings._rotate_proxy_cluster_password(settings)

		self.assertEqual(settings.previous_proxy_cluster_password, "current-password")
		self.assertEqual(settings.proxy_cluster_password, "new-password")
		settings.save.assert_called_once_with(ignore_permissions=True)


class TestRegionalCredentials(UnitTestCase):
	def test_a_save_creates_the_proxy_password_and_the_signing_key(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		settings = MagicMock(is_setup_completed=False)
		with patch("atlas.auth.issuer.initialize_signing_key") as initialize_signing_key:
			AtlasSettings.before_save(settings)

		settings.initialize_proxy_cluster_password.assert_called_once_with()
		initialize_signing_key.assert_called_once_with(settings)


class TestRegionID(UnitTestCase):
	def setUp(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		self.atlas_settings = AtlasSettings
		self.settings = MagicMock()
		self.settings.is_new.return_value = False
		self.settings.has_value_changed.return_value = True
		self.settings.is_setup_completed = False
		self.settings.wildcard_domain = "example.com"
		self.settings.region_name = "test"

	def test_a_virtual_machine_prevents_a_region_id_change(self) -> None:
		with (
			patch("frappe.db.exists", return_value=True),
			self.assertRaises(frappe.ValidationError),
		):
			self.atlas_settings.validate(self.settings)

	def test_a_non_archived_proxy_prevents_a_region_id_change(self) -> None:
		with (
			patch("frappe.db.exists", side_effect=(False, True)),
			self.assertRaises(frappe.ValidationError),
		):
			self.atlas_settings.validate(self.settings)

	def test_archived_proxies_do_not_prevent_a_region_id_change(self) -> None:
		with patch("frappe.db.exists", side_effect=(False, False)):
			self.atlas_settings.validate(self.settings)

		self.settings.server_provider_controller.validate_settings.assert_called_once()


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


class IntegrationTestWildcardCertificate(IntegrationTestCase):
	"""The stored certificate, its private key, and the expiry must stay consistent."""

	def setUp(self) -> None:
		self.settings = frappe.get_single("Atlas Settings")
		self.settings.wildcard_domain = "example.com"
		self.private_key = create_private_key()
		self.certificate = self_signed_certificate(self.private_key, ["*.example.com"])

	def _store(self, certificate: str | None, private_key: str | None) -> None:
		self.settings.wildcard_tls_certificate = certificate
		self.settings.wildcard_tls_private_key = private_key

	def test_the_expiry_is_read_from_the_stored_certificate(self) -> None:
		self._store(self.certificate, serialize_private_key(self.private_key))

		self.settings.validate_wildcard_certificate()

		self.assertGreater(self.settings.wildcard_tls_expires_on, add_days(now_datetime(), 89))

	def test_a_private_key_of_another_certificate_is_refused(self) -> None:
		self._store(self.certificate, serialize_private_key(create_private_key()))

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_wildcard_certificate()

	def test_a_certificate_without_the_private_key_is_refused(self) -> None:
		self._store(self.certificate, None)

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_wildcard_certificate()

	def test_a_certificate_for_another_domain_is_refused(self) -> None:
		other = self_signed_certificate(self.private_key, ["*.other.com"])
		self._store(other, serialize_private_key(self.private_key))

		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_wildcard_certificate()

	def test_clearing_the_certificate_clears_the_key_and_the_expiry(self) -> None:
		self._store(None, serialize_private_key(self.private_key))
		self.settings.wildcard_tls_expires_on = now_datetime()

		self.settings.validate_wildcard_certificate()

		self.assertIsNone(self.settings.wildcard_tls_private_key)
		self.assertIsNone(self.settings.wildcard_tls_expires_on)


class IntegrationTestWildcardCertificateRenewal(IntegrationTestCase):
	def setUp(self) -> None:
		self.settings = frappe.get_single("Atlas Settings")
		self.settings.is_dns_setup_completed = 1
		self.settings.is_wildcard_tls_auto_renew_enabled = 1

	def _renew_expiring(self) -> object:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import renew_expiring_wildcard_certificate

		with (
			patch("frappe.get_single", return_value=self.settings),
			patch("frappe.enqueue_doc") as enqueue_doc,
		):
			renew_expiring_wildcard_certificate()
		return enqueue_doc

	def test_a_certificate_inside_the_renewal_window_is_queued(self) -> None:
		self.settings.wildcard_tls_expires_on = add_days(now_datetime(), 10)

		self.assertTrue(self._renew_expiring().called)

	def test_a_missing_certificate_is_queued(self) -> None:
		self.settings.wildcard_tls_expires_on = None

		self.assertTrue(self._renew_expiring().called)

	def test_a_certificate_outside_the_renewal_window_is_left_alone(self) -> None:
		self.settings.wildcard_tls_expires_on = add_days(now_datetime(), 60)

		self.assertFalse(self._renew_expiring().called)

	def test_nothing_is_queued_when_auto_renew_is_off(self) -> None:
		self.settings.is_wildcard_tls_auto_renew_enabled = 0
		self.settings.wildcard_tls_expires_on = None

		self.assertFalse(self._renew_expiring().called)
