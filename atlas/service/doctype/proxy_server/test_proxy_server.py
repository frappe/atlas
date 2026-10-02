# Copyright (c) 2026, Frappe and Contributors
# See license.txt

import ipaddress
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

import atlas.service.core.proxy.configuration as configuration_module
import atlas.service.doctype.proxy_server.proxy_server as proxy_server_module
from atlas.vm.core.models import FirewallConfiguration


class IntegrationTestProxyServer(IntegrationTestCase):
	def build(self, **values) -> "frappe.Document":
		"""Insert one Proxy Server without running its setup job."""
		proxy_server = frappe.get_doc(
			{
				"doctype": "Proxy Server",
				"virtual_machine": "vm-00001",
			}
			| values
		)
		proxy_server.flags.created_by_proxy_server_api = True
		with (
			patch.object(type(proxy_server), "enqueue_provisioning"),
			patch.object(proxy_server_module.frappe.db, "count", return_value=0),
		):
			proxy_server.insert(ignore_permissions=True, ignore_links=True)
		return proxy_server

	def test_the_name_uses_the_proxy_series(self) -> None:
		proxy_server = self.build()

		self.assertRegex(proxy_server.name, r"^proxy-\d{3}$")

	def test_creation_outside_the_api_is_refused(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Proxy Server", "virtual_machine": "vm-00001"}).insert(
				ignore_permissions=True, ignore_links=True
			)

	def test_setup_is_queued_when_the_record_is_created(self) -> None:
		proxy_server = frappe.get_doc(
			{
				"doctype": "Proxy Server",
				"virtual_machine": "vm-00001",
			}
		)
		proxy_server.flags.created_by_proxy_server_api = True
		with (
			patch.object(type(proxy_server), "enqueue_provisioning") as enqueue_provisioning,
			patch.object(proxy_server_module.frappe.db, "count", return_value=0),
		):
			proxy_server.insert(ignore_permissions=True, ignore_links=True)

		enqueue_provisioning.assert_called_once()

	def test_deletion_is_refused_while_the_machine_exists(self) -> None:
		proxy_server = self.build()

		with self.assertRaises(frappe.ValidationError):
			proxy_server.delete()

	def test_deletion_is_allowed_once_the_machine_is_gone(self) -> None:
		"""Archive detaches the machine. Only then can the record be deleted."""
		proxy_server = self.build()
		proxy_server.db_set({"status": "Archived", "virtual_machine": None})
		proxy_server.reload()

		proxy_server.delete()

		self.assertFalse(frappe.db.exists("Proxy Server", proxy_server.name))

	def test_archive_detaches_the_machine_and_records_its_name(self) -> None:
		"""An archived proxy keeps no machine link."""
		proxy_server = self.build()

		with (
			patch.object(type(proxy_server), "remove_dns_record"),
			patch.object(configuration_module, "push_configuration_to_active_proxies"),
		):
			proxy_server.archive()

		self.assertEqual(proxy_server.status, "Archived")
		self.assertIsNone(proxy_server.virtual_machine)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{"reference_name": proxy_server.name, "content": ("like", "%vm-00001%")},
			)
		)

	def test_archive_terminates_a_machine_that_still_exists(self) -> None:
		proxy_server = self.build()
		virtual_machine = MagicMock()

		with (
			patch.object(type(proxy_server), "remove_dns_record"),
			patch.object(configuration_module, "push_configuration_to_active_proxies"),
			patch.object(proxy_server_module.frappe.db, "exists", return_value=True),
			patch.object(proxy_server_module.frappe, "get_doc", return_value=virtual_machine),
		):
			proxy_server.archive()

		virtual_machine.set_termination_protection.assert_called_once_with(False)
		virtual_machine.terminate.assert_called_once()
		self.assertIsNone(proxy_server.virtual_machine)

	def test_an_action_without_a_machine_is_refused(self) -> None:
		proxy_server = self.build(virtual_machine=None)

		with self.assertRaises(frappe.ValidationError):
			proxy_server.validate_is_reachable()

	def test_reprovision_queues_the_complete_setup_sequence(self) -> None:
		proxy_server = self.build(status="Failed")

		with (
			patch.object(proxy_server_module, "_validate_system_manager"),
			patch.object(proxy_server, "enqueue_provisioning") as enqueue_provisioning,
			patch.object(frappe, "msgprint"),
		):
			proxy_server.provision()

		enqueue_provisioning.assert_called_once()

	def test_a_website_user_cannot_manage_proxies(self) -> None:
		with (
			patch.object(frappe, "only_for"),
			patch.object(frappe, "get_cached_value", return_value="Website User"),
			self.assertRaises(frappe.PermissionError),
		):
			proxy_server_module._validate_system_manager()


class TestProxyServerCreate(UnitTestCase):
	def test_dns_cleanup_removes_the_regional_record_without_an_address(self) -> None:
		proxy_server = MagicMock()
		proxy_server.name = "proxy-001"
		proxy_server.public_ipv4 = None
		proxy_server.dns_health_check_id = "health-1"
		proxy_server.get_domain.return_value = "proxy-001.example.com"
		provider = MagicMock()

		with patch.object(
			proxy_server_module.frappe,
			"get_single",
			return_value=SimpleNamespace(
				dns_provider_controller=provider,
				wildcard_domain="example.com",
			),
		):
			proxy_server_module.ProxyServer.remove_dns_record(proxy_server)

		provider.remove_multivalue_a_record.assert_called_once_with("proxy.example.com", "proxy-001")
		provider.remove_health_check.assert_called_once_with("health-1")

	def test_dns_cleanup_always_removes_the_node_record(self) -> None:
		proxy_server = MagicMock()
		proxy_server.public_ipv4 = None
		proxy_server.dns_health_check_id = None
		proxy_server.get_domain.return_value = "proxy-001.example.com"
		provider = MagicMock()

		with patch.object(
			proxy_server_module.frappe,
			"get_single",
			return_value=SimpleNamespace(
				dns_provider_controller=provider,
				wildcard_domain="example.com",
			),
		):
			proxy_server_module.ProxyServer.remove_dns_record(proxy_server)

		provider.remove_a_record.assert_called_once_with("proxy-001.example.com")

	def test_a_sixth_proxy_is_refused(self) -> None:
		proxy_server = MagicMock()
		proxy_server.flags.created_by_proxy_server_api = True

		with (
			patch.object(proxy_server_module.frappe.db, "count", return_value=5),
			self.assertRaises(frappe.ValidationError),
		):
			proxy_server_module.ProxyServer.before_insert(proxy_server)

	def test_the_creation_api_creates_a_proxy_for_the_new_vm(self) -> None:
		proxy_server = MagicMock(name="proxy_server")
		proxy_server.name = "proxy-001"
		proxy_server.flags = SimpleNamespace()
		virtual_machine_service = MagicMock()
		virtual_machine_service.create.return_value = {"name": "vm-00001", "is_draft": False}

		with (
			patch.object(proxy_server_module.frappe, "only_for"),
			patch.object(proxy_server_module.frappe, "new_doc", return_value=proxy_server),
			patch.object(
				proxy_server_module.frappe,
				"get_single",
				return_value=SimpleNamespace(public_ssh_key="ssh-ed25519 AAAA atlas", region_id=1),
			),
			patch("atlas.vm.core.vm_service.VirtualMachineService", virtual_machine_service),
		):
			name = proxy_server_module.create(
				{
					"virtual_machine_image": "image-1",
					"cpu_millicores": 2000,
					"memory_mib": 4096,
					"disk_mib": 16384,
					"public_ipv4": "32eb57bc-9548-4a89-8358-543e26883569",
				}
			)

		self.assertEqual(name, {"name": "proxy-001", "is_draft": False})
		self.assertEqual(proxy_server.virtual_machine, "vm-00001")
		self.assertEqual(virtual_machine_service.create.call_args.args[0]["tenant_id"], 0)
		self.assertTrue(virtual_machine_service.create.call_args.args[0]["is_privileged"])
		self.assertTrue(virtual_machine_service.create.call_args.args[0]["is_termination_protected"])
		self.assertEqual(virtual_machine_service.create.call_args.args[0]["hostname"], "proxy-001")
		self.assertEqual(
			virtual_machine_service.create.call_args.args[0]["public_ipv4"],
			"32eb57bc-9548-4a89-8358-543e26883569",
		)
		self.assertEqual(virtual_machine_service.create.call_args.args[0]["cpu_millicores"], 2000)
		self.assertEqual(virtual_machine_service.create.call_args.args[0]["memory_mib"], 4096)
		self.assertEqual(virtual_machine_service.create.call_args.args[0]["disk_mib"], 16384)
		proxy_server.enqueue_provisioning.assert_called_once()

	def test_the_proxy_firewall_admits_ssh_only_from_regional_services(self) -> None:
		firewall = FirewallConfiguration.from_value(proxy_server_module.get_proxy_firewall(1))

		def admits(port: int, source: str) -> bool:
			address = ipaddress.ip_address(source)
			return any(
				rule.protocol in {"any", "tcp"}
				and (not rule.ports or rule.ports == str(port))
				and any(address in ipaddress.ip_network(cidr) for cidr in rule.cidrs)
				for rule in firewall.inbound
			)

		self.assertTrue(admits(443, "203.0.113.1"))
		self.assertTrue(admits(22, "fdaa:1::52"))
		self.assertFalse(admits(22, "203.0.113.1"))
		self.assertFalse(admits(22, "fdaa:1:0:7::1"))
		self.assertTrue(admits(443, "fdaa:1:0:7::1"))

	def test_the_record_is_committed_before_the_machine_request(self) -> None:
		proxy_server = MagicMock(name="proxy_server")
		proxy_server.name = "proxy-001"
		proxy_server.flags = SimpleNamespace()
		virtual_machine_service = MagicMock()
		calls: list[str] = []
		proxy_server.insert.side_effect = lambda *_, **__: calls.append("insert")
		virtual_machine_service.create.side_effect = lambda *_, **__: (
			calls.append("create"),
			{"name": "vm-00001", "is_draft": False},
		)[1]

		with (
			patch.object(proxy_server_module.frappe, "only_for"),
			patch.object(proxy_server_module.frappe, "new_doc", return_value=proxy_server),
			patch.object(
				proxy_server_module.frappe,
				"get_single",
				return_value=SimpleNamespace(public_ssh_key="ssh-ed25519 AAAA atlas", region_id=1),
			),
			patch.object(proxy_server_module.frappe.db, "commit", side_effect=lambda: calls.append("commit")),
			patch("atlas.vm.core.vm_service.VirtualMachineService", virtual_machine_service),
		):
			proxy_server_module.create(
				{
					"virtual_machine_image": "image-1",
					"cpu_millicores": 2000,
					"memory_mib": 4096,
					"disk_mib": 16384,
					"public_ipv4": "32eb57bc-9548-4a89-8358-543e26883569",
				}
			)

		self.assertEqual(calls[:3], ["insert", "commit", "create"])

	def test_creation_needs_an_allocated_public_ipv4_address(self) -> None:
		with (
			patch.object(proxy_server_module.frappe, "only_for"),
			self.assertRaisesRegex(frappe.ValidationError, "reserved public IPv4"),
		):
			proxy_server_module.create(
				{
					"virtual_machine_image": "image-1",
					"cpu_millicores": 2000,
					"memory_mib": 4096,
					"disk_mib": 16384,
				}
			)

	def test_pending_proxies_are_queued(self) -> None:
		with (
			patch.object(proxy_server_module.frappe, "get_all", return_value=["proxy-001"]),
			patch.object(proxy_server_module.frappe, "enqueue_doc") as enqueue_doc,
		):
			proxy_server_module.enqueue_pending_proxies_provisioning()

		enqueue_doc.assert_called_once_with(
			"Proxy Server",
			"proxy-001",
			"_provision",
			queue="long",
			timeout=3_600,
			job_id="atlas||proxy-server||provision||proxy-001",
			deduplicate=True,
			enqueue_after_commit=False,
		)
