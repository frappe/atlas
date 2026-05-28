"""Tests for the high-level Task/Connection runner."""

import json
import subprocess
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas._ssh import runner
from atlas.atlas._ssh.transport import Connection
from atlas.atlas.ssh import connection_for_server, execute_task, run_task
from atlas.tests.fixtures import make_provider, make_server

CONNECTION = Connection(
	host="10.0.0.1",
	ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n",
)


def _ok(args, **kwargs) -> subprocess.CompletedProcess:
	return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")


class TestRunTaskArgumentGuard(IntegrationTestCase):
	def test_rejects_both_server_and_connection(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			run_task(
				server="some-server",
				connection=CONNECTION,
				script="phase1-probe.sh",
				variables={},
			)

	def test_rejects_neither_server_nor_connection(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			run_task(script="phase1-probe.sh", variables={})


class TestRunTaskWithServer(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("runner-provider")
		self.server = make_server(
			provider=self.provider,
			title="runner-server",
			ipv4_address="10.0.0.5",
			provider_resource_id="555",
		)

	def test_server_path_builds_connection_from_doc(self) -> None:
		with patch(
			"atlas.atlas._ssh.runner._run_remote_script",
			return_value=("hello\n", "", 0),
		):
			task = run_task(
				server=self.server.name,
				script="phase1-probe.sh",
				variables={"NAME": "hi"},
			)
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.server, self.server.name)


class TestExecuteTask(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("exec-provider")
		self.server = make_server(
			provider=self.provider,
			title="exec-server",
			ipv4_address="10.0.0.6",
			provider_resource_id="556",
		)

	def test_runs_pending_task(self) -> None:
		task = frappe.get_doc({
			"doctype": "Task",
			"server": self.server.name,
			"script": "phase1-probe.sh",
			"variables": json.dumps({"NAME": "hi"}),
			"status": "Pending",
			"triggered_by": "Administrator",
		}).insert(ignore_permissions=True)

		with patch(
			"atlas.atlas._ssh.runner._run_remote_script",
			return_value=("hello\n", "", 0),
		):
			execute_task(task.name)

		task.reload()
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.exit_code, 0)
		self.assertIn("hello", task.stdout)

	def test_raises_when_task_has_no_server(self) -> None:
		task = frappe.get_doc({
			"doctype": "Task",
			"server": None,
			"script": "phase1-probe.sh",
			"variables": json.dumps({}),
			"status": "Pending",
			"triggered_by": "Administrator",
		}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError) as raised:
			execute_task(task.name)
		self.assertIn("no server", str(raised.exception))


class TestConnectionForServer(IntegrationTestCase):
	def test_raises_when_server_has_no_ipv4(self) -> None:
		provider = make_provider("noip-provider")
		server = make_server(
			provider=provider,
			title="noip-server",
			ipv4_address=None,
			provider_resource_id="777",
		)
		with self.assertRaises(frappe.ValidationError) as raised:
			connection_for_server(server)
		self.assertIn("no ipv4_address", str(raised.exception))

	def test_raises_when_atlas_settings_has_no_ssh_private_key_path(self) -> None:
		# connection_for_server now reads ssh_private_key_path from Atlas
		# Settings (not from Server Provider). Clear the field temporarily
		# and confirm the check throws.
		provider = make_provider("noprov-provider")
		server = make_server(
			provider=provider,
			title="noprov-server",
			ipv4_address="10.0.0.99",
			provider_resource_id="888",
		)
		previous = frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path")
		try:
			frappe.db.set_single_value(
				"Atlas Settings", "ssh_private_key_path", "", update_modified=False
			)
			with self.assertRaises(frappe.ValidationError) as raised:
				connection_for_server(server)
			self.assertIn("ssh_private_key_path", str(raised.exception))
		finally:
			frappe.db.set_single_value(
				"Atlas Settings", "ssh_private_key_path", previous, update_modified=False
			)


class TestExceptionWrapping(IntegrationTestCase):
	def test_generic_exception_wrapped_as_validation_error(self) -> None:
		with patch(
			"atlas.atlas._ssh.runner._run_remote_script",
			side_effect=RuntimeError("boom"),
		):
			with self.assertRaises(frappe.ValidationError) as raised:
				run_task(
					connection=CONNECTION,
					script="phase1-probe.sh",
					variables={},
				)
		self.assertIn("boom", str(raised.exception))
		task = frappe.get_last_doc(
			"Task",
			filters={"script": "phase1-probe.sh", "status": "Failure"},
		)
		self.assertEqual(task.status, "Failure")
		self.assertIn("boom", task.stderr)

	def test_validation_error_re_raised_unwrapped(self) -> None:
		inner = frappe.ValidationError("inner")
		with patch(
			"atlas.atlas._ssh.runner._run_remote_script",
			side_effect=inner,
		):
			with self.assertRaises(frappe.ValidationError) as raised:
				run_task(
					connection=CONNECTION,
					script="phase1-probe.sh",
					variables={},
				)
		self.assertIs(raised.exception, inner)


class TestSidecarUploads(IntegrationTestCase):
	def test_sync_image_uploads_sidecars_before_script(self) -> None:
		scp_destinations: list[str] = []

		def capture(args, **kwargs):
			if args[0] == "scp":
				# scp args: ["scp", "-i", key, ...SSH_OPTIONS, local, user@host:remote]
				scp_destinations.append(args[-1])
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=CONNECTION,
				script="sync-image.sh",
				variables={"IMAGE_NAME": "test-image"},
			)

		# The sidecar atlas-network.service is uploaded; the script itself follows.
		self.assertTrue(
			any("atlas-network.service" in destination for destination in scp_destinations),
			f"sidecar not in {scp_destinations}",
		)
		# Script always last, after sidecars.
		script_index = next(
			index for index, destination in enumerate(scp_destinations)
			if destination.endswith("sync-image.sh")
		)
		sidecar_index = next(
			index for index, destination in enumerate(scp_destinations)
			if "atlas-network.service" in destination
		)
		self.assertLess(sidecar_index, script_index)
