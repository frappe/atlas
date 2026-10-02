# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from types import SimpleNamespace
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
		with (
			patch("atlas.auth.issuer.initialize_signing_key") as initialize_signing_key,
			patch("atlas.atlas.core.tls.metal.ensure_certificate_authority") as ensure_certificate_authority,
			patch(
				"atlas.atlas.core.tls.metal.ensure_atlas_client_certificate"
			) as ensure_atlas_client_certificate,
		):
			AtlasSettings.before_save(settings)

		settings.initialize_proxy_cluster_password.assert_called_once_with()
		initialize_signing_key.assert_called_once_with(settings)
		ensure_certificate_authority.assert_called_once_with(settings)
		ensure_atlas_client_certificate.assert_called_once_with(settings)


class TestRegionID(UnitTestCase):
	def setUp(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		self.atlas_settings = AtlasSettings
		self.settings = MagicMock()
		self.settings.is_new.return_value = False
		self.settings.has_value_changed.return_value = True
		self.settings.is_setup_completed = False
		self.settings.placement_strategy = "balanced"
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

	def test_a_region_name_that_breaks_host_titles_is_refused(self) -> None:
		self.atlas_settings._validate_region_name(SimpleNamespace(region_name="par-1"))
		for region_name in ("new_york", "par 1", "a" * 60):
			with self.assertRaisesRegex(frappe.ValidationError, "DNS label"):
				self.atlas_settings._validate_region_name(SimpleNamespace(region_name=region_name))


class TestSleepyVMOvercommitFactor(UnitTestCase):
	def test_valid_factors_are_stored_as_numbers(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		for value in (1, 1.5, "2.5"):
			with self.subTest(value=value):
				settings = SimpleNamespace(sleepy_vm_overcommit_factor=value)
				AtlasSettings._validate_sleepy_vm_overcommit_factor(settings)
				self.assertEqual(settings.sleepy_vm_overcommit_factor, float(value))

	def test_invalid_factors_are_rejected(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		for value in (None, 0, 0.5, -1, float("nan"), float("inf"), float("-inf"), "invalid"):
			with self.subTest(value=value):
				settings = SimpleNamespace(sleepy_vm_overcommit_factor=value)
				with self.assertRaises(frappe.ValidationError):
					AtlasSettings._validate_sleepy_vm_overcommit_factor(settings)


class TestPlacementStrategy(UnitTestCase):
	def test_unknown_strategy_is_rejected(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		settings = MagicMock(placement_strategy="Missing")
		with self.assertRaisesRegex(frappe.ValidationError, "Unknown placement strategy"):
			AtlasSettings.validate(settings)

	def test_options_come_from_the_registry(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		with (
			patch("atlas.vm.core.placement.strategies.base._REGISTERED_STRATEGIES", {"Custom": MagicMock()}),
			patch("frappe.only_for") as only_for,
			patch("frappe.get_all", side_effect=(["large"], ["Ubuntu_26.04"])),
		):
			self.assertEqual(
				AtlasSettings.get_form_autocomplete_options(MagicMock()),
				{
					"placement_strategies": ["Custom"],
					"available_metal_machine_sizes": ["large"],
					"available_metal_machine_images": ["Ubuntu_26.04"],
				},
			)

		only_for.assert_called_once_with("System Manager")


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


class TestObjectStorageConfiguration(UnitTestCase):
	def test_object_storage_needs_a_bucket_and_both_credentials(self) -> None:
		from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

		cases = (
			("bucket", "key", "secret", True),
			("", "key", "secret", False),
			("bucket", "", "secret", False),
			("bucket", "key", None, False),
		)
		for bucket, access_key_id, secret, expected in cases:
			settings = MagicMock(object_storage_bucket=bucket, object_storage_access_key_id=access_key_id)
			settings.get_password.return_value = secret
			with self.subTest(bucket=bucket, access_key_id=access_key_id, secret=secret):
				self.assertEqual(AtlasSettings.is_object_storage_configured.fget(settings), expected)
