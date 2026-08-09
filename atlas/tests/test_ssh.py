import os
import subprocess
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.ssh import Connection, run_task

DUMMY_CONNECTION = Connection(
	host="10.0.0.1",
	ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfakekey\n",
)


def _fake_completed(args, **kwargs) -> subprocess.CompletedProcess:
	# args[0] is "ssh" or "scp". For scp / mkdir we always pretend success.
	if args[0] == "scp":
		return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
	if args[0] == "ssh":
		# The last positional arg is the remote command. mkdir comes first.
		remote_command = args[-1]
		if remote_command.startswith("mkdir"):
			return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
	return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")


class TestRunTask(IntegrationTestCase):
	def setUp(self) -> None:
		# Make sure a real script exists for the run_task path.
		self.script_name = "phase1-probe"

	def test_run_task_success(self) -> None:
		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=_fake_completed):
			task = run_task(
				connection=DUMMY_CONNECTION,
				script=self.script_name,
				variables={"NAME": "hi"},
			)
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.exit_code, 0)
		self.assertIn("ok", task.stdout)

	def test_run_task_failure(self) -> None:
		def fail(args, **kwargs):
			if args[0] == "ssh" and not args[-1].startswith("mkdir"):
				return subprocess.CompletedProcess(args, 2, stdout="", stderr="boom")
			return _fake_completed(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=fail):
			with self.assertRaises(frappe.ValidationError):
				run_task(
					connection=DUMMY_CONNECTION,
					script=self.script_name,
					variables={"NAME": "hi"},
				)
		task = frappe.get_last_doc("Task", filters={"script": self.script_name, "status": "Failure"})
		self.assertEqual(task.exit_code, 2)
		self.assertIn("boom", task.stderr)

	def test_run_task_timeout(self) -> None:
		def timeout(args, **kwargs):
			if args[0] == "ssh" and not args[-1].startswith("mkdir"):
				raise subprocess.TimeoutExpired(cmd=args, timeout=1)
			return _fake_completed(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=timeout):
			with self.assertRaises(frappe.ValidationError):
				run_task(
					connection=DUMMY_CONNECTION,
					script=self.script_name,
					variables={"NAME": "hi"},
					timeout_seconds=1,
				)
		task = frappe.get_last_doc("Task", filters={"script": self.script_name, "status": "Failure"})
		self.assertIn("Timed out", task.stderr)

	def test_run_task_writes_private_key_temp_file_and_deletes_it(self) -> None:
		seen_paths = []

		def capture(args, **kwargs):
			# args[1] is "-i", args[2] is the key path
			if args[0] in ("ssh", "scp"):
				key_path = args[2]
				seen_paths.append(key_path)
				assert os.path.exists(key_path), f"key file missing during call: {key_path}"
				mode = os.stat(key_path).st_mode & 0o777
				assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0o600"
			return _fake_completed(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=DUMMY_CONNECTION,
				script=self.script_name,
				variables={"NAME": "hi"},
			)
		self.assertTrue(seen_paths, "ssh/scp should have been called")
		for path in seen_paths:
			self.assertFalse(os.path.exists(path), f"key file lingered after call: {path}")

	def test_variables_quoted_with_shlex(self) -> None:
		captured_commands = []

		def capture(args, **kwargs):
			if args[0] == "ssh" and args[-1].startswith("env "):
				captured_commands.append(args[-1])
			return _fake_completed(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=DUMMY_CONNECTION,
				script=self.script_name,
				variables={"VAR": "value with spaces"},
			)
		self.assertTrue(captured_commands)
		self.assertIn("VAR='value with spaces'", captured_commands[0])
