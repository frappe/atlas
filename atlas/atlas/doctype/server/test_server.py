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
			carve_virtual_machine_range("2a03:b0c0:abcd:1234::1", "2a03:b0c0:abcd:1234::/64"),
			"2a03:b0c0:abcd:1234::/124",
		)
		self.assertEqual(
			carve_virtual_machine_range("2400:6180:100:d0:0:1:4ae1:d001", "2400:6180:100:d0::/64"),
			"2400:6180:100:d0:0:1:4ae1:d000/124",
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
			stdout='ATLAS_RESULT={"firecracker_version": "", "jailer_version": "", "kernel_version": "", "architecture": ""}',
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

	def test_script_uploads_ship_task_entry_scripts_durably(self) -> None:
		# The Task entry scripts (provision/start/stop/snapshot-stop) ship to
		# /var/lib/atlas/bin so the runner invokes them in place — no per-Task scp.
		from atlas.atlas import scripts_catalog

		destinations = {dest for _src, dest in self.server._script_uploads()}
		for script in ("provision-vm.py", "start-vm.py", "stop-vm.py", "snapshot-stop-vm.py"):
			self.assertIn(f"/var/lib/atlas/bin/{script}", destinations)
		# The durable set covers every host SSH Task entry point.
		for script in scripts_catalog.host_task_scripts():
			self.assertIn(f"/var/lib/atlas/bin/{script}", destinations)

	def test_bootstrap_parses_result_line(self) -> None:
		from atlas.atlas.doctype.server import server as server_module

		# bootstrap-server.py emits one ATLAS_RESULT=<json> line amid trace noise;
		# the controller parses that, not a bare trailing JSON line.
		stdout = (
			"+ some bash trace\n"
			'ATLAS_RESULT={"firecracker_version": "1.15.1",'
			' "jailer_version": "1.15.1",'
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
		self.assertEqual(self.server.jailer_version, "1.15.1")
		self.assertEqual(self.server.kernel_version, "6.8.0-31-generic")
		self.assertEqual(self.server.architecture, "x86_64")

	def test_bootstrap_rejects_from_disallowed_status(self) -> None:
		# `Terminated` is not in BOOTSTRAP_ALLOWED_STATUS. Set in-memory only
		# so the shared server fixture isn't mutated for other tests.
		self.server.status = "Terminated"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.bootstrap()
		self.assertIn("Cannot bootstrap", str(raised.exception))

	def test_get_scripts_returns_operator_visible_scripts(self) -> None:
		from atlas.atlas import scripts_catalog

		entries = self.server.get_scripts()
		# Each entry carries name + intro + fields so the desk Run Task
		# dialog can render itself purely from the response.
		self.assertEqual(
			[entry["name"] for entry in entries],
			scripts_catalog.operator_visible_scripts(),
		)
		for entry in entries:
			self.assertIn("intro", entry)
			self.assertIsInstance(entry["fields"], list)
		# Lifecycle scripts must not leak into the desk picker.
		hidden = {"provision-vm.py", "start-vm.py", "stop-vm.py", "terminate-vm.py", "restart-vm.py"}
		self.assertFalse(hidden & {entry["name"] for entry in entries})


class TestServerArchive(IntegrationTestCase):
	def setUp(self) -> None:
		# Reset so each test starts from a non-Archived state.
		frappe.db.delete("Server", {"title": "test-server-archive"})
		provider = make_provider("test-provider-archive")
		self.server = make_server(
			provider,
			"test-server-archive",
			provider_resource_id="44",
			ipv4_address="10.0.0.50",
			ipv6_address="2a03:b0c0:abcd:9999::1",
			ipv6_prefix="2a03:b0c0:abcd:9999::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:9999::/124",
			status="Active",
		)

	def test_archive_sets_status_archived(self) -> None:
		from unittest.mock import MagicMock, patch

		with patch("atlas.atlas.atlas_settings.providers.for_provider", return_value=MagicMock()):
			self.server.archive()
		self.assertEqual(
			frappe.db.get_value("Server", self.server.name, "status"),
			"Archived",
		)

	def test_archive_throws_when_already_archived(self) -> None:
		from unittest.mock import MagicMock, patch

		with patch("atlas.atlas.atlas_settings.providers.for_provider", return_value=MagicMock()):
			self.server.archive()
		self.server.reload()
		with self.assertRaises(frappe.ValidationError):
			self.server.archive()


class TestServerSyncImage(IntegrationTestCase):
	def test_sync_image_delegates_to_image_controller(self) -> None:
		from atlas.tests.fixtures import make_image

		provider = make_provider("test-provider-sync")
		server = make_server(
			provider,
			"test-server-sync",
			provider_resource_id="55",
			ipv4_address="10.0.0.55",
			ipv6_address="2a03:b0c0:abcd:8888::1",
			ipv6_prefix="2a03:b0c0:abcd:8888::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:8888::/124",
			status="Active",
		)
		image = make_image("test-image-sync")
		with patch("frappe.enqueue"):
			task_name = server.sync_image(image.name)
		task = frappe.get_doc("Task", task_name)
		self.assertEqual(task.script, "sync-image.py")
		self.assertEqual(task.server, server.name)


class TestServerImmutability(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete("Server", {"title": "test-server-immut"})
		provider = make_provider("test-provider-immut")
		self.server = make_server(
			provider,
			"test-server-immut",
			provider_resource_id="66",
			ipv4_address="10.0.0.66",
			ipv6_address="2a03:b0c0:abcd:7777::1",
			ipv6_prefix="2a03:b0c0:abcd:7777::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:7777::/124",
			status="Active",
		)

	def test_provider_is_immutable_once_set(self) -> None:
		other_provider = make_provider("other-provider-immut")
		self.server.provider = other_provider.name
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.save(ignore_permissions=True)
		self.assertIn("provider is immutable", str(raised.exception))

	def test_title_is_immutable_once_set(self) -> None:
		self.server.reload()
		self.server.title = "renamed-server"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.server.save(ignore_permissions=True)
		self.assertIn("title is immutable", str(raised.exception))

	def test_name_is_a_uuid(self) -> None:
		import uuid

		# Round-trip the UUID parser: raises if `name` isn't a UUID.
		uuid.UUID(self.server.name)

	def test_ipv4_can_be_set_when_initially_blank(self) -> None:
		"""DigitalOcean provision flow: server starts Pending with no IPs;
		`finish_provisioning` later writes them. The immutability check
		should allow None → value transitions."""
		# Reset so the test is hermetic across re-runs (the previous run
		# would have set ipv4_address, which set_only_once then locks).
		frappe.db.delete("Server", {"title": "test-server-blank"})
		blank = make_server(
			make_provider("test-provider-blank"),
			"test-server-blank",
			provider_resource_id="77",
			status="Pending",
		)
		blank.ipv4_address = "10.0.0.77"
		blank.save(ignore_permissions=True)
		blank.reload()
		self.assertEqual(blank.ipv4_address, "10.0.0.77")
