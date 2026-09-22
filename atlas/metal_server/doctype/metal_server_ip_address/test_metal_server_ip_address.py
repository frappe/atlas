from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ReservedIPAddress
from atlas.metal_server.core.ip_address_service import UNOWNED_TENANT_ID
from atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address import (
	IPAddressIntent,
	MetalServerIPAddress,
)


class TestServerIPAddress(UnitTestCase):
	def test_reserved_ip_address_is_frozen(self) -> None:
		reserved = ReservedIPAddress("203.0.113.10", "provider-id")

		with self.assertRaises(FrozenInstanceError):
			reserved.address = "203.0.113.11"

	def test_an_address_without_a_tenant_goes_to_the_shared_pool(self) -> None:
		"""An unset tenant uses the shared-pool sentinel."""
		address = frappe.get_doc(
			{
				"doctype": "Metal Server IP Address",
				"address": "203.0.113.10",
				"provider_resource_id": "provider-1",
			}
		)

		with patch("frappe.get_single", return_value=SimpleNamespace(server_provider="AWS")):
			address.validate()

		self.assertEqual(address.tenant_id, UNOWNED_TENANT_ID)
		self.assertEqual(address.provider_resource_id, "provider-1")

	def test_generic_address_is_its_own_provider_resource_id(self) -> None:
		address = frappe.get_doc(
			{
				"doctype": "Metal Server IP Address",
				"address": "203.0.113.10",
				"provider_resource_id": "incorrect-resource-id",
			}
		)

		with patch("frappe.get_single", return_value=SimpleNamespace(server_provider="Generic")):
			address.validate()

		self.assertEqual(address.provider_resource_id, "203.0.113.10")

	def test_reset_tenant_returns_the_address_to_the_pool(self) -> None:
		address = SimpleNamespace(
			address="203.0.113.10", tenant_id=7, release_to_pool=Mock(), add_comment=Mock()
		)

		with patch("frappe.only_for") as only_for:
			MetalServerIPAddress.reset_tenant(address)

		only_for.assert_called_once_with("System Manager")
		address.release_to_pool.assert_called_once()

	def test_reset_tenant_records_the_losing_tenant(self) -> None:
		address = SimpleNamespace(
			address="203.0.113.10", tenant_id=7, release_to_pool=Mock(), add_comment=Mock()
		)

		with patch("frappe.only_for"):
			MetalServerIPAddress.reset_tenant(address)

		address.add_comment.assert_called_once()
		kind, message = address.add_comment.call_args.args
		self.assertEqual(kind, "Info")
		self.assertIn("7", message)

	def test_reset_tenant_needs_a_system_manager(self) -> None:
		address = SimpleNamespace(
			address="203.0.113.10", tenant_id=7, release_to_pool=Mock(), add_comment=Mock()
		)

		with patch("frappe.only_for", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				MetalServerIPAddress.reset_tenant(address)

		address.release_to_pool.assert_not_called()

	def test_an_unowned_address_is_already_in_the_pool(self) -> None:
		address = SimpleNamespace(
			address="203.0.113.10", tenant_id=-1, release_to_pool=Mock(), add_comment=Mock()
		)

		with patch("frappe.only_for"), self.assertRaises(frappe.ValidationError):
			MetalServerIPAddress.reset_tenant(address)

		address.release_to_pool.assert_not_called()

	def test_assignment_increments_the_intent_version(self) -> None:
		address = SimpleNamespace(
			name="203.0.113.10",
			status="Allocated",
			server=None,
			virtual_machine=None,
			host_address=None,
			intent_version=4,
			save=Mock(),
			queue_reconcile=Mock(),
		)

		MetalServerIPAddress.begin_assignment(address, SimpleNamespace(name="node-1"), "VM-00001")

		self.assertEqual(address.intent_version, 5)
		self.assertEqual(address.status, "Attaching")
		address.queue_reconcile.assert_called_once()

	def test_an_unreserved_address_leaves_the_tenant_on_release(self) -> None:
		address = SimpleNamespace(
			name="203.0.113.10",
			status="Attached",
			server=None,
			virtual_machine="VM-00001",
			host_address=None,
			tenant_id=7,
			reserved=0,
			intent_version=1,
			save=Mock(),
			queue_reconcile=Mock(),
		)

		MetalServerIPAddress.release(address)

		self.assertEqual(address.status, "Allocated")
		self.assertEqual(address.tenant_id, UNOWNED_TENANT_ID)

	def test_a_reserved_address_keeps_its_tenant_on_release(self) -> None:
		address = SimpleNamespace(
			name="203.0.113.10",
			status="Attached",
			server=None,
			virtual_machine="VM-00001",
			host_address=None,
			tenant_id=7,
			reserved=1,
			intent_version=1,
			save=Mock(),
			queue_reconcile=Mock(),
		)

		MetalServerIPAddress.release(address)

		self.assertEqual(address.status, "Allocated")
		self.assertEqual(address.tenant_id, 7)

	def test_a_detach_returns_an_unreserved_address_to_the_pool(self) -> None:
		for reserved, expects_pool_return in ((False, True), (True, False)):
			intent = IPAddressIntent(
				version=3,
				status="Detaching",
				provider_resource_id="provider-id",
				server="node-1",
				reserved=reserved,
				public_address="203.0.113.10",
				host_address="10.1.8.55",
				virtual_machine=None,
			)
			query = Mock()
			query.set.return_value = query
			query.where.return_value = query
			query_builder = Mock(DocType=Mock(), update=Mock(return_value=query))

			with (
				patch(
					"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.qb",
					new=query_builder,
				),
				patch(
					"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.db.get_value",
					return_value=None,
				),
			):
				MetalServerIPAddress.complete_intent(
					SimpleNamespace(doctype="Metal Server IP Address", name="203.0.113.10"),
					intent,
					None,
				)

			tenant_writes = [call for call in query.set.call_args_list if call.args[1] == UNOWNED_TENANT_ID]
			self.assertEqual(bool(tenant_writes), expects_pool_return)

	def test_reconcile_applies_one_intent_per_job(self) -> None:
		intent = IPAddressIntent(
			1,
			"Attaching",
			"provider-id",
			"node-1",
			True,
			"203.0.113.10",
			None,
			"VM-00001",
		)
		worker = SimpleNamespace(
			doctype="Metal Server IP Address",
			name="203.0.113.10",
			apply_intent=Mock(return_value="10.1.8.55"),
			complete_intent=Mock(return_value=True),
			send_address_to_metal=Mock(),
		)

		with patch(
			"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.get_doc",
			return_value=SimpleNamespace(get_intent=Mock(return_value=intent)),
		):
			MetalServerIPAddress.reconcile(worker)

		worker.apply_intent.assert_called_once_with(intent)
		worker.complete_intent.assert_called_once_with(intent, worker.apply_intent.return_value)
		worker.send_address_to_metal.assert_called_once_with("VM-00001", "10.1.8.55")

	def test_reconcile_does_not_send_a_stale_intent_to_metal(self) -> None:
		intent = IPAddressIntent(
			1,
			"Attaching",
			"provider-id",
			"node-1",
			True,
			"203.0.113.10",
			None,
			"VM-00001",
		)
		worker = SimpleNamespace(
			doctype="Metal Server IP Address",
			name="203.0.113.10",
			apply_intent=Mock(return_value="10.1.8.55"),
			complete_intent=Mock(return_value=False),
			send_address_to_metal=Mock(),
		)

		with patch(
			"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.get_doc",
			return_value=SimpleNamespace(get_intent=Mock(return_value=intent)),
		):
			MetalServerIPAddress.reconcile(worker)

		worker.send_address_to_metal.assert_not_called()

	def test_reconcile_logs_the_resource_intent_and_version(self) -> None:
		intent = IPAddressIntent(
			7,
			"Attaching",
			"provider-id",
			"node-1",
			True,
			"203.0.113.10",
			None,
			"VM-00001",
		)
		worker = SimpleNamespace(
			doctype="Metal Server IP Address",
			name="203.0.113.10",
			apply_intent=Mock(side_effect=RuntimeError("provider failed")),
			complete_intent=Mock(),
		)

		with (
			patch(
				"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.get_doc",
				return_value=SimpleNamespace(get_intent=Mock(return_value=intent)),
			),
			patch(
				"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.log_error"
			) as log_error,
			self.assertRaisesRegex(RuntimeError, "provider failed"),
		):
			MetalServerIPAddress.reconcile(worker)

		self.assertEqual(
			log_error.call_args.kwargs["title"],
			"Metal Server IP Address 203.0.113.10 Attaching intent 7 failed",
		)
		worker.complete_intent.assert_not_called()

	def test_attach_needs_an_ipv4_host_address(self) -> None:
		intent = IPAddressIntent(
			1,
			"Attaching",
			"provider-id",
			"node-1",
			True,
			"203.0.113.10",
			None,
			"VM-00001",
		)
		provider = Mock()
		provider.attach_public_ipv4_address.return_value = "not-an-address"

		with (
			patch(
				"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.get_single",
				return_value=SimpleNamespace(server_provider_controller=provider),
			),
			patch(
				"atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.frappe.get_doc"
			),
			self.assertRaisesRegex(ValueError, "IPv4 host address"),
		):
			MetalServerIPAddress.apply_intent(SimpleNamespace(), intent)
