from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask


class TestServerSSHTask(UnitTestCase):
	def test_create_for_command_records_the_command(self) -> None:
		log = Mock()
		log.insert.return_value = log

		with patch(
			"atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.get_doc", return_value=log
		) as get_doc:
			result = ServerSSHTask.create_for_command(
				server="node-test-00001",
				command="echo done",
				environment={"MESSAGE": "done"},
			)

		self.assertIs(result, log)
		self.assertEqual(json.loads(get_doc.call_args.args[0]["environment"]), {"MESSAGE": "done"})
		self.assertEqual(get_doc.call_args.args[0]["script"], "echo done")
		self.assertEqual(get_doc.call_args.args[0]["ssh_user"], "root")
		log.insert.assert_called_once_with(ignore_permissions=True)
		self.assertTrue(log.flags.run_in_background)

	def test_create_for_script_file_records_the_script_source(self) -> None:
		log = Mock()
		log.insert.return_value = log

		with patch(
			"atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.get_doc", return_value=log
		) as get_doc:
			result = ServerSSHTask.create_for_script_file(
				server="node-test-00001",
				script_path="scaleway/configure-private-network.sh",
				run_in_background=False,
			)

		self.assertIs(result, log)
		self.assertIn("netplan generate", get_doc.call_args.args[0]["script"])
		self.assertFalse(log.flags.run_in_background)

	def test_create_for_script_file_rejects_a_missing_script_file(self) -> None:
		with self.assertRaises(ValueError):
			ServerSSHTask.create_for_script_file(server="node-test-00001", script_path="no-such-script.sh")

	def test_after_insert_queues_by_default(self) -> None:
		log = Mock()
		log.flags = SimpleNamespace()

		ServerSSHTask.after_insert(log)

		log._enqueue.assert_called_once()

	def test_after_insert_runs_inline_when_requested(self) -> None:
		log = Mock()
		log.flags = SimpleNamespace(run_in_background=False)

		ServerSSHTask.after_insert(log)

		log.execute.assert_called_once()

	def test_enqueue_uses_the_log_timeout(self) -> None:
		log = SimpleNamespace(
			doctype="Server SSH Task",
			name="SSH-00001",
			timeout_seconds=120,
		)

		with patch("atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.enqueue_doc") as enqueue:
			ServerSSHTask._enqueue(log)

		self.assertEqual(enqueue.call_args.kwargs["timeout"], 120)

	def test_execute_records_an_ssh_error_as_a_failed_result(self) -> None:
		log = SimpleNamespace(
			server="node-test-00001",
			port=22,
			ssh_user="root",
			script="echo done",
			_mark_running=Mock(),
			_environment=Mock(return_value={}),
			_finish=Mock(),
		)

		with (
			patch("atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.get_doc"),
			patch(
				"atlas.atlas.core.ssh.SSHRunner",
				side_effect=OSError("offline"),
			),
		):
			result = ServerSSHTask.execute(log)

		self.assertIsNone(result.exit_code)
		self.assertEqual(result.output, "offline")
		log._finish.assert_called_once_with(result)

	def test_append_output_writes_progress(self) -> None:
		log = SimpleNamespace(output="first\n", db_set=Mock())

		with patch("atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.db.commit"):
			ServerSSHTask._append_output(log, "second\n")

		self.assertEqual(log.output, "first\nsecond\n")
		log.db_set.assert_called_once_with("output", log.output, update_modified=False)

	def test_mark_timed_out_marks_an_expired_running_task_as_failed(self) -> None:
		now = datetime(2026, 9, 1, 12, 0, 11)
		log = SimpleNamespace(
			status="Running",
			started_at=now - timedelta(seconds=21),
			timeout_seconds=10,
			timeout_buffer_seconds=10,
			output="progress",
			db_set=Mock(),
		)

		with patch("atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.db.commit"):
			changed = ServerSSHTask.mark_timed_out(log, now)

		self.assertTrue(changed)
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.ended_at, now)
		self.assertIn("timed out", log.output)

	def test_mark_timed_out_keeps_a_task_inside_the_timeout_buffer(self) -> None:
		now = datetime(2026, 9, 1, 12, 0, 10)
		log = SimpleNamespace(
			status="Running",
			started_at=now - timedelta(seconds=20),
			timeout_seconds=10,
			timeout_buffer_seconds=10,
		)

		self.assertFalse(ServerSSHTask.mark_timed_out(log, now))

	def test_finish_does_not_replace_a_timed_out_log(self) -> None:
		log = SimpleNamespace(doctype="Server SSH Task", name="SSH-00001", db_set=Mock())

		with patch(
			"atlas.server.doctype.server_ssh_task.server_ssh_task.frappe.db.get_value", return_value="Failed"
		):
			ServerSSHTask._finish(log, SimpleNamespace(output="done", is_success=True, exit_code=0))

		log.db_set.assert_not_called()
