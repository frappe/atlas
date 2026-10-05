from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerPowerAction, UnsupportedProviderOperation
from atlas.atlas.core.server_providers.generic import GenericError, GenericProvider
from atlas.atlas.core.server_providers.registry import get_server_provider
from atlas.patches.move_redfish_to_generic_bmc import execute


class TestGenericProvider(UnitTestCase):
	def test_registry_returns_the_generic_provider(self) -> None:
		for driver in (None, "SSH"):
			with self.subTest(driver=driver):
				provider = get_server_provider(
					"Generic", settings=self.settings(generic_provider_driver=driver)
				)
				self.assertIs(type(provider), GenericProvider)
				self.assertFalse(provider.is_registration_only)

	def test_unknown_driver_is_rejected(self) -> None:
		with self.assertRaisesRegex(GenericError, "Unknown Generic provider driver"):
			get_server_provider("Generic", settings=self.settings(generic_provider_driver="IPMI"))

	def test_provider_migration_is_scoped_and_repeatable(self) -> None:
		for provider, driver, expected in (
			("Redfish", "SSH", ("Generic", "BMC")),
			("Generic", None, ("Generic", "SSH")),
			("Generic", "BMC", ("Generic", "BMC")),
			("AWS", "BMC", ("AWS", "BMC")),
		):
			with self.subTest(provider=provider, driver=driver):
				frappe.db.savepoint("generic_driver_migration")
				try:
					frappe.db.set_single_value(
						"Atlas Settings", {"server_provider": provider, "generic_provider_driver": driver}
					)
					before = frappe.db.get_singles_dict("Atlas Settings")
					for _ in range(2):
						execute()
						after = frappe.db.get_singles_dict("Atlas Settings")
						self.assertEqual((after.server_provider, after.generic_provider_driver), expected)
						for field in before.keys() - {
							"server_provider",
							"generic_provider_driver",
							"modified",
							"modified_by",
						}:
							self.assertEqual(after[field], before[field])
				finally:
					frappe.db.rollback(save_point="generic_driver_migration")
					frappe.db.value_cache.clear()

	def test_settings_reject_automatic_host_creation(self) -> None:
		with self.assertRaisesRegex(GenericError, "automatically"):
			GenericProvider(self.settings(auto_spawn_metal_server=1)).validate_settings()

	def test_server_needs_a_registered_storage_pool_device(self) -> None:
		with self.assertRaisesRegex(GenericError, "storage pool device"):
			GenericProvider(self.settings()).validate_server(self.server(provider_metadata="{}"))

	def test_server_creation_is_refused(self) -> None:
		with self.assertRaises(UnsupportedProviderOperation):
			GenericProvider(self.settings()).ensure_server(Mock())

	def test_power_action_is_refused(self) -> None:
		with self.assertRaises(UnsupportedProviderOperation):
			GenericProvider(self.settings()).set_power_state("host-1", ServerPowerAction.REBOOT)

	def test_network_setup_only_checks_the_private_address(self) -> None:
		provider = GenericProvider(self.settings())
		server = self.server()

		with (
			patch.object(provider, "wait_for_private_address") as wait_for_private_address,
			patch.object(provider, "run_setup_script") as run_setup_script,
		):
			provider.configure_server_network(server)

		wait_for_private_address.assert_called_once_with(server)
		run_setup_script.assert_not_called()

	def test_storage_pool_device_is_the_registered_device(self) -> None:
		self.assertEqual(
			GenericProvider(self.settings()).get_storage_pool_device(self.server()), "/dev/nvme1n1"
		)

	def test_public_address_attaches_as_itself(self) -> None:
		provider = GenericProvider(self.settings())

		host_address = provider.attach_public_ip_address("203.0.113.9", "203.0.113.9", self.server())

		self.assertEqual(host_address, "203.0.113.9")

	def test_public_address_needs_its_own_resource_id(self) -> None:
		provider = GenericProvider(self.settings())

		with self.assertRaisesRegex(GenericError, "resource ID"):
			provider.attach_public_ip_address("provider-address-1", "203.0.113.9", self.server())

	def test_public_address_reservation_is_refused(self) -> None:
		with self.assertRaises(UnsupportedProviderOperation):
			GenericProvider(self.settings()).reserve_public_ip_address(4)

	@staticmethod
	def settings(**values: object) -> SimpleNamespace:
		return SimpleNamespace(**{"auto_spawn_metal_server": 0, **values})

	@staticmethod
	def server(**values: object) -> SimpleNamespace:
		return SimpleNamespace(
			**{"name": "host-1", "provider_metadata": '{"storage_pool_device": "/dev/nvme1n1"}', **values}
		)
