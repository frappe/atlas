from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import carve_virtual_machine_range
from atlas.atlas.ssh import Connection
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_provider, make_server


class TestNetworking(IntegrationTestCase):
	def test_carve_virtual_machine_range(self) -> None:
		self.assertEqual(
			carve_virtual_machine_range("2a03:b0c0:abcd:1234::/64"),
			"2a03:b0c0:abcd:1234::/124",
		)
		self.assertEqual(
			carve_virtual_machine_range("2001:db8::/64"),
			"2001:db8::/124",
		)


class TestServerBootstrap(IntegrationTestCase):
	def setUp(self) -> None:
		provider = make_provider("test-provider-server")
		self.server = make_server(
			provider,
			"test-server-bootstrap",
			provider_resource_id="1",
			ipv4_address="10.0.0.5",
			ipv6_address="2a03:b0c0:abcd:1234::1",
			ipv6_prefix="2a03:b0c0:abcd:1234::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:1234::/124",
			status="Bootstrapping",
		)

	def test_bootstrap_uploads_helpers_then_runs_script(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		task = fake_task(
			name="task-x",
			stdout='{"firecracker_version": "", "kernel_version": "", "architecture": ""}',
		)

		with patch.object(server_module, "upload_files") as upload:
			with patch.object(server_module, "run_task", return_value=task) as run:
				with patch.object(
					server_module,
					"connection_for_server",
					return_value=Connection(host="x", ssh_private_key="k"),
				):
					self.server.bootstrap()

		upload.assert_called_once()
		run.assert_called_once()

	def test_bootstrap_parses_trailing_json_line(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		stdout = (
			"+ some bash trace\n"
			'{"firecracker_version": "1.15.1",'
			' "kernel_version": "6.8.0-31-generic",'
			' "architecture": "x86_64"}\n'
		)
		task = fake_task(name="task-y", stdout=stdout)

		with patch.object(server_module, "upload_files"):
			with patch.object(server_module, "run_task", return_value=task):
				with patch.object(
					server_module,
					"connection_for_server",
					return_value=Connection(host="x", ssh_private_key="k"),
				):
					self.server.bootstrap()
		self.server.reload()
		self.assertEqual(self.server.firecracker_version, "1.15.1")
		self.assertEqual(self.server.kernel_version, "6.8.0-31-generic")
		self.assertEqual(self.server.architecture, "x86_64")

	def test_bootstrap_rejects_from_disallowed_status(self) -> None:
		# `Terminated` is not in BOOTSTRAP_ALLOWED_STATUS. Set in-memory only
		# so the shared server fixture isn't mutated for other tests.
		self.server.status = "Terminated"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.bootstrap()
		self.assertIn("Cannot bootstrap", str(raised.exception))

	def test_get_scripts_returns_allowed_scripts(self) -> None:
		from atlas.atlas import scripts_catalog

		expected = scripts_catalog.allowed_scripts()
		self.assertEqual(self.server.get_scripts(), expected)
