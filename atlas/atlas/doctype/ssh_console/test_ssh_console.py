"""Controller coverage for SSH Console.execute and the worker fan-out wiring."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import ssh_console as engine
from atlas.atlas.doctype.ssh_console import ssh_console as controller
from atlas.tests.fixtures import make_server


class TestSSHConsoleExecute(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = make_server(title="ssh-console-srv", ipv4_address="10.0.0.9")

	def _console(self, command="uname -a", targets=None):
		if targets is None:
			targets = (("Server", self.server.name),)
		console = frappe.get_single("SSH Console")
		console.command = command
		console.nonce = "test-nonce"
		console.set("targets", [])
		for kind, name in targets:
			console.append("targets", {"target_doctype": kind, "target_name": name})
		console.set("results", [])
		return console

	def test_empty_command_rejected(self) -> None:
		console = self._console(command="   ")
		with self.assertRaises(frappe.exceptions.ValidationError):
			console.execute()

	def test_no_targets_rejected(self) -> None:
		console = self._console(targets=())
		with self.assertRaises(frappe.exceptions.ValidationError):
			console.execute()

	def test_execute_creates_running_log_and_enqueues(self) -> None:
		console = self._console()
		with patch.object(controller.frappe, "enqueue") as enqueue:
			result = console.execute()

		log = frappe.get_doc("SSH Command Log", result["log"])
		self.assertEqual(log.status, "Running")
		self.assertEqual(log.command, "uname -a")
		self.assertEqual(log.target_count, 1)
		self.assertEqual(result["nonce"], "test-nonce")

		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(
			enqueue.call_args.args[0],
			"atlas.atlas.doctype.ssh_console.ssh_console._execute_console",
		)
		self.assertEqual(kwargs["log_name"], result["log"])
		self.assertEqual(kwargs["targets"], [("Server", self.server.name)])
		self.assertTrue(kwargs["enqueue_after_commit"])


class TestExecuteConsoleWorker(IntegrationTestCase):
	def setUp(self) -> None:
		self.server_a = make_server(title="ssh-worker-a", ipv4_address="10.0.0.10")
		self.server_b = make_server(title="ssh-worker-b", ipv4_address="10.0.0.11")

	def test_worker_streams_results_and_finalizes_failure(self) -> None:
		log = frappe.get_doc(
			{
				"doctype": "SSH Command Log",
				"command": "false",
				"status": "Running",
				"target_count": 2,
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

		def fake_fan_out(targets, command, *, on_result, timeout_seconds):
			results = []
			for index, target in enumerate(targets):
				status = engine.FAILURE if index == 1 else engine.SUCCESS
				result = engine.CommandResult(
					target_kind=target.kind,
					target_name=target.name,
					status=status,
					stdout="out",
					stderr="",
					exit_code=0 if status == engine.SUCCESS else 1,
					duration_milliseconds=3,
				)
				results.append(result)
				on_result(result)
			return results

		with (
			patch.object(engine, "run_fan_out", side_effect=fake_fan_out),
			patch.object(controller, "_publish"),
		):
			controller._execute_console(
				log_name=log.name,
				targets=[("Server", self.server_a.name), ("Server", self.server_b.name)],
				command="false",
				timeout_seconds=5,
				nonce="n",
				user="Administrator",
			)

		log.reload()
		self.assertEqual(log.status, "Failure")
		self.assertEqual(len(log.results), 2)
		self.assertEqual(log.results[1].status, "Failure")
		self.assertEqual(log.duration_milliseconds, 6)
