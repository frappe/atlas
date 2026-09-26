from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.core.proxy.provisioning as provisioning
from atlas.atlas.core.ssh import SSHResult
from atlas.service.core.proxy.provisioning import ProxyServerProvisioner


def _proxy_server(**values) -> SimpleNamespace:
	defaults = {
		"doctype": "Proxy Server",
		"name": "proxy-001",
		"status": "Pending",
		"virtual_machine": "vm-00001",
		"public_ipv4": None,
		"get_domain": lambda: "proxy-001.example.com",
		"installed_package_hash": None,
		"pushed_config_hash": None,
		"tls_expires_on": None,
		"dns_health_check_id": None,
		"is_provisioning_completed": 0,
		"failure_message": None,
		"save": Mock(),
	}
	proxy_server = SimpleNamespace(**(defaults | values))
	return proxy_server


class TestProvisioningSequence(UnitTestCase):
	def test_the_steps_install_before_they_configure(self) -> None:
		"""One setup run installs the command that applies the configuration."""
		phases = [phase for phase, _operation in ProxyServerProvisioner(_proxy_server()).steps]

		self.assertEqual(
			phases,
			[
				"virtual-machine",
				"dns",
				"secure-shell",
				"package",
				"configuration",
				"cluster-membership",
				"control-readiness",
				"global-dns",
			],
		)

	def test_a_failed_step_records_its_phase_and_stops(self) -> None:
		proxy_server = _proxy_server()
		provisioner = ProxyServerProvisioner(proxy_server)
		later_step = Mock()
		with (
			patch.object(ProxyServerProvisioner, "is_virtual_machine_ready", new=property(lambda self: True)),
			patch.object(
				ProxyServerProvisioner,
				"steps",
				new=property(
					lambda self: (
						("virtual-machine", Mock(side_effect=RuntimeError("not ready"))),
						("secure-shell", later_step),
					)
				),
			),
			patch.object(provisioning.frappe.db, "commit"),
			patch.object(provisioning.frappe, "log_error"),
			patch.object(provisioner, "save_progress"),
			self.assertRaises(RuntimeError),
		):
			provisioner.run()

		self.assertEqual(proxy_server.status, "Failed")
		self.assertIn("virtual-machine", proxy_server.failure_message)
		later_step.assert_not_called()

	def test_a_step_defers_its_save_to_the_provisioning_sequence(self) -> None:
		proxy_server = _proxy_server()
		provisioner = ProxyServerProvisioner(proxy_server)
		operation = Mock()

		provisioner.run_step("package", operation)

		operation.assert_called_once_with(save=False)
		proxy_server.save.assert_called_once_with(ignore_permissions=True)

	def test_a_draft_virtual_machine_keeps_provisioning_pending(self) -> None:
		proxy_server = _proxy_server()
		provisioner = ProxyServerProvisioner(proxy_server)
		with patch.object(
			provisioning.frappe, "get_doc", return_value=SimpleNamespace(name="vm-00001", is_draft=True)
		):
			provisioner.run()

		self.assertEqual(proxy_server.status, "Pending")
		proxy_server.save.assert_not_called()


class TestApplySteps(UnitTestCase):
	def test_readiness_is_confirmed_before_dns_publication(self) -> None:
		proxy_server = _proxy_server()
		provisioner = ProxyServerProvisioner(proxy_server)
		responses = [SimpleNamespace(status_code=503), SimpleNamespace(status_code=204)]

		with (
			patch.object(provisioning.requests, "get", side_effect=responses) as request,
			patch.object(provisioning.time, "sleep"),
		):
			provisioner.wait_for_control_readiness()

		self.assertEqual(request.call_count, 2)
		request.assert_called_with("https://proxy-001.example.com/readyz", timeout=2)
		proxy_server.save.assert_called_once_with(ignore_permissions=True)

	def test_cluster_membership_reaches_active_peers(self) -> None:
		joining_proxy = _proxy_server(name="proxy-003")
		active_proxy = _proxy_server(name="proxy-001", status="Active")
		provisioner = ProxyServerProvisioner(joining_proxy)

		with (
			patch.object(provisioning.frappe, "get_all", return_value=["proxy-001"]),
			patch.object(provisioning.frappe, "get_doc", return_value=active_proxy),
			patch.object(ProxyServerProvisioner, "push_configuration") as push_configuration,
		):
			provisioner.push_cluster_membership()

		push_configuration.assert_called_once()
		joining_proxy.save.assert_called_once_with(ignore_permissions=True)

	def test_an_unchanged_configuration_is_not_sent_again(self) -> None:
		proxy_server = _proxy_server(pushed_config_hash="digest-1")
		provisioner = ProxyServerProvisioner(proxy_server)
		with (
			patch.object(provisioning, "ProxyConfiguration") as configuration,
			patch.object(provisioning, "SSHRunner") as ssh_runner,
		):
			configuration.return_value.digest = "digest-1"

			provisioner.push_configuration()

		ssh_runner.assert_not_called()
		proxy_server.save.assert_called_once_with(ignore_permissions=True)

	def test_a_rejected_configuration_file_fails_loudly(self) -> None:
		proxy_server = _proxy_server()
		provisioner = ProxyServerProvisioner(proxy_server)
		with (
			patch.object(provisioning, "ProxyConfiguration") as configuration,
			patch.object(provisioning, "SSHRunner") as ssh_runner,
			patch.object(provisioning.SSHTask, "create_for_command") as create_task,
			patch.object(provisioner.__class__, "ssh_host", new=property(lambda self: "203.0.113.9")),
		):
			configuration.return_value.digest = "digest-2"
			ssh_runner.return_value.run_command.return_value = SSHResult("no space left", 1)

			with self.assertRaises(frappe.ValidationError):
				provisioner.push_configuration()

		create_task.assert_not_called()
		self.assertIsNone(proxy_server.pushed_config_hash)

	# The task keeps the error available in Desk.
	def test_a_failed_apply_step_names_its_ssh_task(self) -> None:
		proxy_server = _proxy_server(virtual_machine="vm-00001")
		provisioner = ProxyServerProvisioner(proxy_server)
		with (
			patch.object(provisioning, "ProxyConfiguration") as configuration,
			patch.object(provisioning, "SSHRunner") as ssh_runner,
			patch.object(provisioning.SSHTask, "create_for_command") as create_task,
			patch.object(provisioner.__class__, "ssh_host", new=property(lambda self: "203.0.113.9")),
		):
			configuration.return_value.digest = "digest-3"
			ssh_runner.return_value.run_command.return_value = SSHResult("", 0)
			create_task.return_value = SimpleNamespace(name="task-1", result=SSHResult("bad certificate", 1))

			with self.assertRaises(frappe.ValidationError) as error:
				provisioner.push_configuration()

		self.assertIn("task-1", str(error.exception))
		self.assertIsNone(proxy_server.pushed_config_hash)

	def test_an_installed_package_is_not_installed_again(self) -> None:
		proxy_server = _proxy_server(installed_package_hash="sha-1", virtual_machine="vm-00001")
		provisioner = ProxyServerProvisioner(proxy_server)
		with (
			patch.object(
				provisioning.frappe,
				"get_single",
				return_value={"http_proxy_package_file": "file-1", "http_proxy_package_hash": "sha-1"},
			),
			patch(
				"atlas.service.core.service_package.get_download_url",
				return_value="https://atlas.test/files/file-1",
			),
			patch.object(provisioning.SSHTask, "create_for_script_file") as create_task,
		):
			provisioner.install_package()

		create_task.assert_not_called()
		proxy_server.save.assert_called_once_with(ignore_permissions=True)

	def test_a_missing_package_fails_loudly(self) -> None:
		provisioner = ProxyServerProvisioner(_proxy_server())
		with patch.object(provisioning.frappe, "get_single", return_value={}):
			with self.assertRaises(frappe.ValidationError):
				provisioner.install_package()


class TestDNSRecord(UnitTestCase):
	def build(self, **values):
		proxy_server = _proxy_server(**values)
		provisioner = ProxyServerProvisioner(proxy_server)
		self.provider = MagicMock()
		patches = (
			patch.object(
				provisioning.frappe,
				"get_single",
				return_value=SimpleNamespace(
					dns_provider_controller=self.provider,
					wildcard_domain="example.com",
				),
			),
			patch.object(provisioning.frappe, "get_doc", return_value=proxy_server),
		)
		for patched in patches:
			patched.start()
			self.addCleanup(patched.stop)
		return proxy_server, provisioner

	def test_the_name_points_at_the_reserved_address(self) -> None:
		_, provisioner = self.build(public_ipv4="203.0.113.9")

		provisioner.update_dns_record()

		self.provider.upsert_a_record.assert_called_once_with(
			"proxy-001.example.com", "203.0.113.9", ttl=3600
		)

	def test_a_missing_address_fails_loudly(self) -> None:
		_proxy, provisioner = self.build(public_ipv4=None)

		with self.assertRaises(frappe.ValidationError):
			provisioner.update_dns_record()

		self.provider.upsert_a_record.assert_not_called()

	def test_an_archived_proxy_does_not_restore_its_record(self) -> None:
		proxy_server, provisioner = self.build(status="Archived", public_ipv4="203.0.113.9")

		provisioner.update_dns_record()

		self.assertIs(provisioner.proxy_server, proxy_server)
		self.provider.upsert_a_record.assert_not_called()

	def test_the_global_record_has_a_health_check(self) -> None:
		proxy_server, provisioner = self.build(public_ipv4="203.0.113.9")
		self.provider.create_https_health_check.return_value = "health-1"

		provisioner.update_global_dns_record()

		self.provider.create_https_health_check.assert_called_once_with(
			"203.0.113.9", "proxy-001.example.com", "/healthz"
		)
		self.provider.upsert_multivalue_a_record.assert_called_once_with(
			"proxy.example.com", "proxy-001", "203.0.113.9", "health-1", ttl=120
		)
		self.provider.upsert_cname_record.assert_called_once_with(
			"*.example.com", "proxy.example.com", ttl=3600
		)
		self.assertEqual(proxy_server.dns_health_check_id, "health-1")
