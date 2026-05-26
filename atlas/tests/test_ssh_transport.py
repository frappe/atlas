"""Tests for the low-level SSH/SCP transport helpers."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas._ssh import transport
from atlas.atlas._ssh.transport import (
	Connection,
	_ensure_known_hosts_directory,
	run_scp,
	ssh_key_file,
	upload_files,
	wait_for_ssh,
)

CONNECTION = Connection(
	host="10.0.0.1",
	ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n",
)


def _ok(args, **kwargs) -> subprocess.CompletedProcess:
	return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestWaitForSsh(IntegrationTestCase):
	def test_returns_when_ssh_ready(self) -> None:
		with patch(
			"atlas.atlas._ssh.transport.run_ssh",
			return_value=("", "", 0),
		):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				wait_for_ssh(CONNECTION, timeout_seconds=10)

	def test_times_out_when_never_ready(self) -> None:
		with patch(
			"atlas.atlas._ssh.transport.run_ssh",
			return_value=("", "", 255),
		):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				with patch(
					"atlas.atlas._ssh.transport.time.monotonic",
					side_effect=[0.0, 1.0, 9999.0],
				):
					with self.assertRaises(frappe.ValidationError):
						wait_for_ssh(CONNECTION, timeout_seconds=10)


class TestUploadFiles(IntegrationTestCase):
	def test_empty_list_is_noop(self) -> None:
		with patch("atlas.atlas._ssh.transport.subprocess.run") as run:
			upload_files(CONNECTION, [])
		run.assert_not_called()

	def test_skips_mkdir_when_remote_files_have_no_parent_dir(self) -> None:
		# A bare basename (no slash) has dirname == "" — the set
		# comprehension filters it, leaving no dirs to mkdir.
		commands: list[list[str]] = []

		def capture(args, **kwargs):
			commands.append(list(args))
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			upload_files(CONNECTION, [("/tmp/x.sh", "x.sh")])

		# No `mkdir` call; first call is the scp.
		self.assertEqual(commands[0][0], "scp")

	def test_creates_remote_dirs_then_scps_each_file(self) -> None:
		commands: list[list[str]] = []

		def capture(args, **kwargs):
			commands.append(list(args))
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			upload_files(
				CONNECTION,
				[
					("/tmp/a.sh", "/remote/dir1/a.sh"),
					("/tmp/b.sh", "/remote/dir2/b.sh"),
				],
			)

		# First call: ssh mkdir -p ... .
		first_command = commands[0]
		self.assertEqual(first_command[0], "ssh")
		self.assertIn("mkdir -p", first_command[-1])
		self.assertIn("/remote/dir1", first_command[-1])
		self.assertIn("/remote/dir2", first_command[-1])

		# Subsequent calls: scp for each file.
		scp_calls = [command for command in commands[1:] if command[0] == "scp"]
		self.assertEqual(len(scp_calls), 2)


class TestRunScp(IntegrationTestCase):
	def test_raises_on_non_zero_exit(self) -> None:
		def failed(args, **kwargs):
			return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=failed):
			with self.assertRaises(frappe.ValidationError) as raised:
				run_scp(CONNECTION, "/tmp/key", "/local/a", "/remote/a", timeout_seconds=30)
		self.assertIn("permission denied", str(raised.exception))


class TestSshKeyFile(IntegrationTestCase):
	def test_writes_key_with_0600_and_deletes_on_exit(self) -> None:
		with ssh_key_file("-----BEGIN-----\ndata\n") as path:
			self.assertTrue(os.path.exists(path))
			mode = os.stat(path).st_mode & 0o777
			self.assertEqual(mode, 0o600)
			with open(path) as file:
				self.assertIn("data", file.read())
		self.assertFalse(os.path.exists(path))

	def test_appends_trailing_newline_when_missing(self) -> None:
		with ssh_key_file("no-newline") as path:
			with open(path) as file:
				self.assertTrue(file.read().endswith("\n"))

	def test_swallows_unlink_error_on_exit(self) -> None:
		# Pre-delete the file inside the context; exiting must not raise.
		with ssh_key_file("data\n") as path:
			os.unlink(path)
			self.assertFalse(os.path.exists(path))
		# If we got here without raising, the OSError was swallowed as expected.


class TestEnsureKnownHostsDirectory(IntegrationTestCase):
	def test_creates_missing_parent_with_0700(self) -> None:
		with tempfile.TemporaryDirectory() as temp_directory:
			fake_path = Path(temp_directory) / "atlas" / "known_hosts"
			with patch("atlas.atlas._ssh.transport.KNOWN_HOSTS_PATH", fake_path):
				_ensure_known_hosts_directory()
			parent = fake_path.parent
			self.assertTrue(parent.exists())
			mode = parent.stat().st_mode & 0o777
			self.assertEqual(mode, 0o700)

	def test_no_op_when_parent_exists(self) -> None:
		with tempfile.TemporaryDirectory() as temp_directory:
			fake_path = Path(temp_directory) / "known_hosts"
			# Parent already exists (the temp_directory itself).
			with patch("atlas.atlas._ssh.transport.KNOWN_HOSTS_PATH", fake_path):
				_ensure_known_hosts_directory()
			self.assertTrue(fake_path.parent.exists())
