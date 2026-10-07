from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.setup import AtlasSetup, AtlasSetupConfiguration


def configuration(**changes: object) -> AtlasSetupConfiguration:
	values = {
		"server_provider": "Scaleway",
		"dns_provider": "Route53",
		"region_name": "par-1",
		"region_id": 1,
		"wildcard_domain": "par-1.example.com",
		"private_network_cidr": "10.1.0.0/20",
		"private_network_mtu": 1500,
		"central_jwks_url": "",
		"public_ssh_key": "ssh-ed25519 AAAA atlas",
		"scaleway_organization_id": "organization",
		"scaleway_project_id": "project",
		"scaleway_zone": "fr-par-1",
		"scaleway_machine_billing_cycle": "Hourly",
		"scaleway_access_key": "access",
		"scaleway_secret_key": "secret",
		"route53_access_key_id": "route-access",
		"route53_access_key_secret": "route-secret",
		"letsencrypt_email": "ops@example.com",
		"is_letsencrypt_staging": False,
		"is_wildcard_tls_auto_renew_enabled": True,
		"use_dedicated_sleepy_vm_hosts": False,
		"placement_strategy": "balanced",
		"sleepy_vm_overcommit_factor": 1.0,
		"auto_spawn_metal_server": False,
		"default_metal_machine_size": "",
		"default_metal_machine_image": "",
		"warpgate_url": "",
		"warpgate_api_token": "",
		"warpgate_api_token_id": "",
	}
	values.update(changes)
	return AtlasSetupConfiguration.from_dict(values)


def generic_configuration() -> AtlasSetupConfiguration:
	values = configuration().settings_values()
	for field in tuple(values):
		if field.startswith("scaleway_"):
			values.pop(field)
	values["server_provider"] = "Generic"
	return AtlasSetupConfiguration.from_dict(values)


def aws_configuration(**changes: object) -> AtlasSetupConfiguration:
	values = configuration().settings_values()
	for field in tuple(values):
		if field.startswith("scaleway_"):
			values.pop(field)
	values.update(
		{
			"server_provider": "AWS",
			"aws_region": "eu-west-1",
			"aws_availability_zone": "eu-west-1a",
			"aws_access_key_id": "key-id",
			"aws_secret_access_key": "key-secret",
		}
	)
	values.update(changes)
	return AtlasSetupConfiguration.from_dict(values)


class TestAtlasSetupConfiguration(UnitTestCase):
	def test_input_needs_every_field(self) -> None:
		values = configuration().settings_values()
		values.pop("region_id")

		with self.assertRaisesRegex(ValueError, "missing fields: region_id"):
			AtlasSetupConfiguration.from_dict(values)

	def test_input_rejects_a_wrong_scalar_type(self) -> None:
		values = configuration().settings_values()
		values["private_network_mtu"] = "1500"

		with self.assertRaisesRegex(ValueError, "private_network_mtu must be an integer"):
			AtlasSetupConfiguration.from_dict(values)

	def test_region_name_is_normalized(self) -> None:
		self.assertEqual(configuration(region_name=" PAR-1 ").region_name, "par-1")

	def test_sleepy_vm_overcommit_factor_is_normalized(self) -> None:
		self.assertEqual(configuration(sleepy_vm_overcommit_factor=2).sleepy_vm_overcommit_factor, 2.0)

	def test_auto_spawn_needs_both_metal_catalog_names(self) -> None:
		with self.assertRaisesRegex(ValueError, "default_metal_machine_size is required"):
			configuration(auto_spawn_metal_server=True)

	def test_aws_input_carries_only_the_aws_fields(self) -> None:
		values = aws_configuration().settings_values()

		self.assertEqual(values["aws_availability_zone"], "eu-west-1a")
		self.assertNotIn("scaleway_zone", values)
		self.assertEqual(AtlasSetupConfiguration.from_dict(values).server_provider, "AWS")

	def test_a_provider_cannot_carry_the_fields_of_another_provider(self) -> None:
		values = aws_configuration().settings_values()
		values["scaleway_zone"] = "fr-par-1"

		with self.assertRaisesRegex(ValueError, "unknown fields: scaleway_zone"):
			AtlasSetupConfiguration.from_dict(values)

	def test_an_unknown_server_provider_is_rejected(self) -> None:
		values = configuration().settings_values()
		values["server_provider"] = "GCP"

		with self.assertRaisesRegex(ValueError, "server_provider must be one of"):
			AtlasSetupConfiguration.from_dict(values)


class TestAtlasSetup(UnitTestCase):
	def setup(self, settings: object, **changes: object) -> AtlasSetup:
		with patch("atlas.atlas.core.setup.frappe.get_single", return_value=settings):
			return AtlasSetup(configuration(**changes))

	def test_setup_keeps_old_warpgate_token_until_new_token_is_committed(self) -> None:
		setup = MagicMock(spec=AtlasSetup)
		setup.settings = MagicMock()
		with (
			patch("atlas.atlas.core.setup.publish_warpgate_ui"),
			patch("atlas.atlas.core.setup.WarpgateTokenManager") as token_manager,
			patch("atlas.atlas.core.setup.frappe.db.commit", side_effect=RuntimeError("commit failed")),
		):
			with self.assertRaisesRegex(RuntimeError, "commit failed"):
				AtlasSetup.run(setup)

		token_manager.return_value.delete_other_tokens.assert_not_called()

	def test_an_aws_region_uses_unicast_networking(self) -> None:
		settings = MagicMock(is_unicast_network_enabled=0)
		with patch("atlas.atlas.core.setup.frappe.get_single", return_value=settings):
			setup = AtlasSetup(aws_configuration())

		setup._apply_settings()

		self.assertEqual(settings.is_unicast_network_enabled, 1)
		settings.save.assert_called_once_with(ignore_permissions=True)

	def test_cloud_providers_route_automatic_ipv6(self) -> None:
		for provider_configuration in (configuration(), aws_configuration()):
			settings = MagicMock(use_ipv6_router_for_auto_assignment=0)
			with patch("atlas.atlas.core.setup.frappe.get_single", return_value=settings):
				setup = AtlasSetup(provider_configuration)

			setup._apply_settings()

			self.assertEqual(settings.use_ipv6_router_for_auto_assignment, 1)

	def test_generic_provider_keeps_direct_automatic_ipv6(self) -> None:
		settings = MagicMock(use_ipv6_router_for_auto_assignment=0)
		with patch("atlas.atlas.core.setup.frappe.get_single", return_value=settings):
			setup = AtlasSetup(generic_configuration())

		setup._apply_settings()

		self.assertEqual(settings.use_ipv6_router_for_auto_assignment, 0)

	def test_generic_provider_completes_without_a_catalog(self) -> None:
		settings = MagicMock(is_setup_completed=1, wildcard_tls_expires_on="2027-01-01")
		with patch("atlas.atlas.core.setup.frappe.get_single", return_value=settings):
			setup = AtlasSetup(generic_configuration())

		with patch("atlas.atlas.core.setup.frappe.db.count", return_value=0):
			setup._validate_result()

	def test_completed_provider_refuses_immutable_drift(self) -> None:
		settings = MagicMock(is_server_provider_setup_completed=1, is_dns_setup_completed=0)
		settings.get.side_effect = lambda field: (
			"old-region" if field == "region_name" else getattr(configuration(), field)
		)
		setup = self.setup(settings)

		with self.assertRaises(frappe.ValidationError):
			setup._validate_immutable_values()

	def test_missing_public_zone_stops_before_dns_bootstrap(self) -> None:
		provider = MagicMock()
		provider.find_public_zone_id.return_value = None
		settings = SimpleNamespace(
			dns_provider_controller=provider,
			wildcard_domain="par-1.example.com",
			route53_dns_zone_id=None,
			is_dns_setup_completed=0,
		)
		setup = self.setup(settings)

		with self.assertRaises(frappe.ValidationError):
			setup._setup_dns_provider()

		provider.bootstrap.assert_not_called()

	def test_existing_public_zone_completes_dns_setup(self) -> None:
		provider = MagicMock()
		provider.find_public_zone_id.return_value = "zone-1"
		settings = SimpleNamespace(
			dns_provider_controller=provider,
			wildcard_domain="par-1.example.com",
			route53_dns_zone_id=None,
			is_dns_setup_completed=0,
		)
		setup = self.setup(settings)

		with patch("atlas.atlas.core.setup.frappe.db.commit") as commit:
			setup._setup_dns_provider()

		self.assertEqual(settings.route53_dns_zone_id, "zone-1")
		provider.bootstrap.assert_called_once_with()
		commit.assert_called_once_with()

	def test_current_certificate_is_not_replaced(self) -> None:
		settings = MagicMock(wildcard_tls_expires_on=frappe.utils.add_days(frappe.utils.now_datetime(), 60))
		setup = self.setup(settings)

		setup._issue_wildcard_certificate()

		settings.issue_wildcard_certificate.assert_not_called()

	def test_environment_change_clears_the_current_certificate(self) -> None:
		settings = SimpleNamespace(
			is_letsencrypt_staging=1,
			wildcard_tls_certificate="staging certificate",
			wildcard_tls_private_key="staging key",
		)
		setup = self.setup(settings, is_letsencrypt_staging=False)

		setup._clear_certificate_for_environment_change()

		self.assertIsNone(settings.wildcard_tls_certificate)
		self.assertIsNone(settings.wildcard_tls_private_key)

	def test_central_key_sync_reloads_settings(self) -> None:
		settings = MagicMock()
		setup = self.setup(settings)

		with patch("atlas.atlas.core.setup.sync_central_jwks", return_value=True):
			setup._sync_central_keys()

		settings.reload.assert_called_once_with()

	def test_setup_continues_without_central_keys(self) -> None:
		settings = MagicMock()
		setup = self.setup(settings)

		with patch("atlas.atlas.core.setup.sync_central_jwks", return_value=False):
			setup._sync_central_keys()

		settings.reload.assert_called_once_with()
