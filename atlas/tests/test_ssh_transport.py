"""Tests for the low-level SSH/SCP transport helpers."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas._ssh import transport
from atlas.atlas._ssh._quote import substitute
from atlas.atlas._ssh.transport import (
	PROBE_CONNECT_TIMEOUT_SECONDS,
	Connection,
	_ensure_known_hosts_directory,
	forget_host,
	run_detached,
	run_scp,
	run_ssh,
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

	def test_forgets_recycled_host_key_before_polling(self) -> None:
		# A freshly-(re)created VM may land on a recycled IP whose stale key we
		# pinned; wait_for_ssh must drop it first so accept-new re-pins the new
		# key instead of hard-failing on a changed key (real-provision-traps #1).
		with patch("atlas.atlas._ssh.transport.forget_host") as forget:
			with patch("atlas.atlas._ssh.transport.run_ssh", return_value=("", "", 0)):
				with patch("atlas.atlas._ssh.transport.time.sleep"):
					wait_for_ssh(CONNECTION, timeout_seconds=10)
		forget.assert_called_once_with(CONNECTION.host)

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

	def test_probe_timeout_is_retried_not_fatal(self) -> None:
		# A guest still booting isn't listening on :22 yet, so the per-probe ssh
		# hangs until its own timeout fires -> subprocess.TimeoutExpired. That must
		# be folded into the poll loop like a non-zero exit, NOT escape and fail the
		# whole provision on the first probe (the bug that marked a Site Failed ~30s
		# in, defeating the timeout_seconds budget).
		responses = [subprocess.TimeoutExpired(cmd="ssh", timeout=30), ("", "", 0)]

		def flaky(*args, **kwargs):
			outcome = responses.pop(0)
			if isinstance(outcome, Exception):
				raise outcome
			return outcome

		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=flaky):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				wait_for_ssh(CONNECTION, timeout_seconds=10)
		self.assertEqual(responses, [])

	def test_persistent_probe_timeout_raises_validation_not_raw_timeout(self) -> None:
		# If the guest never comes up, the per-probe timeouts must still surface as
		# the wait's own ValidationError at the deadline — never a raw TimeoutExpired
		# leaking out on the first probe.
		with patch(
			"atlas.atlas._ssh.transport.run_ssh",
			side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30),
		):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				with patch(
					"atlas.atlas._ssh.transport.time.monotonic",
					side_effect=[0.0, 1.0, 9999.0],
				):
					with self.assertRaises(frappe.ValidationError):
						wait_for_ssh(CONNECTION, timeout_seconds=10)

	def test_probe_overrides_connect_timeout_to_90s(self) -> None:
		# This step gets a longer ConnectTimeout than the shared 30s default so a
		# slow-booting guest gets a longer single connect attempt. The override must
		# reach run_ssh, and the subprocess timeout must be >= it so it can't kill
		# ssh before its own connect attempt finishes.
		with patch("atlas.atlas._ssh.transport.run_ssh", return_value=("", "", 0)) as run_ssh:
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				wait_for_ssh(CONNECTION, timeout_seconds=10)
		_, kwargs = run_ssh.call_args
		self.assertEqual(kwargs["extra_options"], [f"ConnectTimeout={PROBE_CONNECT_TIMEOUT_SECONDS}"])
		self.assertGreaterEqual(kwargs["timeout_seconds"], PROBE_CONNECT_TIMEOUT_SECONDS)


class TestRunDetached(IntegrationTestCase):
	"""The long-build detach helper: launch under setsid+nohup, poll a marker, read
	the log. Drives the launch/poll mechanics both the bench bake and the proxy
	build now share."""

	def test_launches_detached_then_returns_log_and_exit_on_marker(self) -> None:
		# Sequence: launch (rc 0), first poll returns the exit-code marker, then the
		# log read. time.sleep is no-op'd so the poll loop doesn't actually wait.
		responses = [("", "", 0), ("0\n", "", 0), ("BUILD LOG", "", 0)]
		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=responses) as run_ssh:
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				log, _stderr, code = run_detached(
					CONNECTION, "/tmp/key", "/x/build.sh", log_path="/x/build.log", done_path="/x/build.done"
				)
		self.assertEqual((log, code), ("BUILD LOG", 0))
		# The launch command detaches the build so a dropped SSH can't SIGHUP it.
		launch = run_ssh.call_args_list[0].args[2]
		self.assertIn("setsid", launch)
		self.assertIn("nohup", launch)
		self.assertIn("/x/build.sh", launch)

	def test_propagates_nonzero_build_exit(self) -> None:
		responses = [("", "", 0), ("1\n", "", 0), ("oops", "", 0)]
		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=responses):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				_log, _stderr, code = run_detached(
					CONNECTION, "/tmp/key", "/x/build.sh", log_path="/x/build.log", done_path="/x/build.done"
				)
		self.assertEqual(code, 1)

	def test_transient_poll_failure_is_retried_not_fatal(self) -> None:
		# A dropped poll (run_ssh raises) must not abort the wait — the next poll
		# finds the marker. launch ok, poll raises, poll returns "0", log read.
		calls = {"n": 0}

		def flaky(connection, key_path, command, *params, timeout_seconds, **kwargs):
			calls["n"] += 1
			if calls["n"] == 1:
				return ("", "", 0)  # launch
			if calls["n"] == 2:
				raise OSError("connection reset")  # dropped poll
			if calls["n"] == 3:
				return ("0\n", "", 0)  # marker present
			return ("LOG", "", 0)  # log read

		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=flaky):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				log, _stderr, code = run_detached(
					CONNECTION, "/tmp/key", "/x/build.sh", log_path="/x/build.log", done_path="/x/build.done"
				)
		self.assertEqual((log, code), ("LOG", 0))

	def test_raises_when_build_overruns_overall_timeout(self) -> None:
		# Marker never appears; monotonic jumps past the deadline → raise.
		with patch("atlas.atlas._ssh.transport.run_ssh", return_value=("", "", 0)):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				with patch("atlas.atlas._ssh.transport.time.monotonic", side_effect=[0.0, 1.0, 9999.0]):
					with self.assertRaises(frappe.ValidationError):
						run_detached(
							CONNECTION,
							"/tmp/key",
							"/x/build.sh",
							log_path="/x/build.log",
							done_path="/x/build.done",
							overall_timeout_seconds=10,
						)


class TestRunDetachedStreaming(IntegrationTestCase):
	"""The opt-in `on_log` live-tail (spec/22): each poll reads the bytes the build
	appended since the last read (`tail -c +<offset>`) and hands them to the sink.
	With no sink the call sequence is unchanged (covered by TestRunDetached)."""

	def test_no_sink_makes_no_tail_calls(self) -> None:
		# The default path must be byte-for-byte the old behavior: launch, poll
		# marker, read log — three run_ssh calls, no `tail -c`.
		responses = [("", "", 0), ("0\n", "", 0), ("FULL LOG", "", 0)]
		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=responses) as run_ssh:
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				run_detached(
					CONNECTION, "/tmp/key", "/x/build.sh", log_path="/x/build.log", done_path="/x/build.done"
				)
		commands = [call.args[2] for call in run_ssh.call_args_list]
		self.assertEqual(len(commands), 3)
		self.assertFalse(any("tail -c" in command for command in commands))

	def test_streams_growing_tail_by_offset(self) -> None:
		# Two poll rounds before the marker, the log growing each time. The sink must
		# receive each new chunk exactly once (offset advances by chunk length), and
		# the offset request must use the running 1-indexed `tail -c +N`.
		chunks: list[str] = []
		# launch, [poll: not-done, tail "aaa"], [poll: not-done, tail "bbb"],
		# [poll: done "0", tail "" (final drain), full-log read]
		responses = [
			("", "", 0),  # launch
			("", "", 0),  # poll 1: done marker empty
			("aaa\n", "", 0),  # poll 1: tail
			("", "", 0),  # poll 2: done marker empty
			("bbb\n", "", 0),  # poll 2: tail
			("0\n", "", 0),  # poll 3: done marker present
			("", "", 0),  # poll 3: final-drain tail (nothing new)
			("aaa\nbbb\n", "", 0),  # full log read on completion
		]
		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=responses) as run_ssh:
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				log, _stderr, code = run_detached(
					CONNECTION,
					"/tmp/key",
					"/x/build.sh",
					log_path="/x/build.log",
					done_path="/x/build.done",
					on_log=chunks.append,
				)
		self.assertEqual((log, code), ("aaa\nbbb\n", 0))
		self.assertEqual(chunks, ["aaa\n", "bbb\n"])
		# Offsets: first tail at +1 (whole file), second at +5 (after "aaa\n" = 4 bytes).
		# run_ssh now takes a (template, *params) command with {} holes, so render each
		# call back to the line it would have run before asserting the offset.
		tail_commands = [
			substitute(c.args[2], c.args[3:]) for c in run_ssh.call_args_list if "tail -c" in c.args[2]
		]
		self.assertIn("tail -c +1 ", tail_commands[0])
		self.assertIn("tail -c +5 ", tail_commands[1])

	def test_sink_exception_does_not_kill_build_and_offset_holds(self) -> None:
		# A throwing sink must not propagate (it would kill a build it only observes)
		# and must not advance the offset (so the chunk is retried, not dropped).
		seen: list[str] = []

		def angry(chunk: str) -> None:
			if not seen:
				seen.append(chunk)
				raise RuntimeError("sink boom")
			seen.append(chunk)

		responses = [
			("", "", 0),  # launch
			("", "", 0),  # poll 1: done empty
			("hello", "", 0),  # poll 1: tail -> sink raises, offset stays
			("0\n", "", 0),  # poll 2: done present
			("hello", "", 0),  # poll 2: final-drain tail -> same window retried
			("hello", "", 0),  # full log read
		]
		with patch("atlas.atlas._ssh.transport.run_ssh", side_effect=responses):
			with patch("atlas.atlas._ssh.transport.time.sleep"):
				log, _stderr, code = run_detached(
					CONNECTION,
					"/tmp/key",
					"/x/build.sh",
					log_path="/x/build.log",
					done_path="/x/build.done",
					on_log=angry,
				)
		# Build still completes; the chunk that raised was re-offered, not lost.
		self.assertEqual((log, code), ("hello", 0))
		self.assertEqual(seen, ["hello", "hello"])


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


class TestEnsuresKnownHostsBeforeConnecting(IntegrationTestCase):
	"""run_ssh and run_scp must create ~/.atlas before invoking ssh/scp:
	StrictHostKeyChecking=accept-new writes the new host key into
	KNOWN_HOSTS_PATH, so the parent must exist or ssh warns and drops the key.
	Pushing the guard into these two helpers (rather than relying on callers)
	is what lets the proxy control plane (atlas.atlas.proxy) SSH guests safely —
	it doesn't go through the runner that used to ensure this."""

	def test_run_ssh_ensures_known_hosts_dir(self) -> None:
		with patch("atlas.atlas._ssh.transport._ensure_known_hosts_directory") as ensure:
			with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=_ok):
				run_ssh(CONNECTION, "/tmp/key", "true", timeout_seconds=30)
		ensure.assert_called_once()

	def test_run_scp_ensures_known_hosts_dir(self) -> None:
		with patch("atlas.atlas._ssh.transport._ensure_known_hosts_directory") as ensure:
			with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=_ok):
				run_scp(CONNECTION, "/tmp/key", "/local/a", "/remote/a", timeout_seconds=30)
		ensure.assert_called_once()


class TestRunSsh(IntegrationTestCase):
	def test_extra_options_append_after_defaults_so_a_duplicate_wins(self) -> None:
		# ssh honours the LAST occurrence of a repeated -o, so an override must come
		# after the shared SSH_OPTIONS (which already carries ConnectTimeout=30).
		captured = {}

		def capture(args, **kwargs):
			captured["args"] = list(args)
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_ssh(CONNECTION, "/tmp/key", "true", timeout_seconds=90, extra_options=["ConnectTimeout=90"])

		args = captured["args"]
		default = args.index("ConnectTimeout=30")
		override = args.index("ConnectTimeout=90")
		self.assertLess(default, override)  # the 90s override is last → it wins
		# The override is a proper `-o key=value` pair, and the remote command is
		# still the final arg (override slotted before host/command, not after).
		self.assertEqual(args[override - 1], "-o")
		self.assertEqual(args[-1], "true")

	def test_no_extra_options_leaves_argv_at_defaults(self) -> None:
		captured = {}

		def capture(args, **kwargs):
			captured["args"] = list(args)
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_ssh(CONNECTION, "/tmp/key", "true", timeout_seconds=30)

		# Only the default ConnectTimeout=30 is present; no override leaked in.
		self.assertEqual(captured["args"].count("ConnectTimeout=30"), 1)
		self.assertNotIn("ConnectTimeout=90", captured["args"])


class TestRunScp(IntegrationTestCase):
	def test_raises_on_non_zero_exit(self) -> None:
		def failed(args, **kwargs):
			return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=failed):
			with self.assertRaises(frappe.ValidationError) as raised:
				run_scp(CONNECTION, "/tmp/key", "/local/a", "/remote/a", timeout_seconds=30)
		self.assertIn("permission denied", str(raised.exception))

	def test_ipv4_destination_is_unbracketed(self) -> None:
		captured = {}

		def capture(args, **kwargs):
			captured["args"] = args
			return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_scp(CONNECTION, "/tmp/key", "/local/a", "/remote/a", timeout_seconds=30)
		self.assertEqual(captured["args"][-1], "root@10.0.0.1:/remote/a")

	def test_ipv6_destination_is_bracketed(self) -> None:
		# scp's host:path syntax splits on the first colon, so a v6 literal (a
		# guest /128) must be bracketed or scp mangles the address — the bug that
		# broke the first real guest-SSH-over-v6 (proxy build_proxy scp).
		v6 = Connection(host="2400:6180:100:d0:0:1:517f:8002", ssh_private_key=CONNECTION.ssh_private_key)
		captured = {}

		def capture(args, **kwargs):
			captured["args"] = args
			return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_scp(v6, "/tmp/key", "/local/a", "/remote/a", timeout_seconds=30)
		self.assertEqual(captured["args"][-1], "root@[2400:6180:100:d0:0:1:517f:8002]:/remote/a")


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


class TestForgetHost(IntegrationTestCase):
	def test_noop_when_known_hosts_missing(self) -> None:
		with tempfile.TemporaryDirectory() as temp_directory:
			fake_path = Path(temp_directory) / "known_hosts"  # never created
			with patch("atlas.atlas._ssh.transport.KNOWN_HOSTS_PATH", fake_path):
				with patch("atlas.atlas._ssh.transport.subprocess.run") as run:
					forget_host("10.0.0.1")
			run.assert_not_called()

	def test_runs_keygen_remove_against_known_hosts(self) -> None:
		with tempfile.TemporaryDirectory() as temp_directory:
			fake_path = Path(temp_directory) / "known_hosts"
			fake_path.write_text("")  # exists → forget proceeds
			captured: dict = {}

			def capture(args, **kwargs):
				captured["args"] = list(args)
				return _ok(args, **kwargs)

			with patch("atlas.atlas._ssh.transport.KNOWN_HOSTS_PATH", fake_path):
				with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
					forget_host("10.0.0.1")
			self.assertEqual(captured["args"][:3], ["ssh-keygen", "-R", "10.0.0.1"])
			self.assertIn(str(fake_path), captured["args"])

	def test_strips_brackets_from_v6_literal(self) -> None:
		# We bracket v6 for scp's host:path syntax; ssh-keygen -R wants the bare
		# literal (default port), matching what accept-new wrote.
		with tempfile.TemporaryDirectory() as temp_directory:
			fake_path = Path(temp_directory) / "known_hosts"
			fake_path.write_text("")
			captured: dict = {}

			def capture(args, **kwargs):
				captured["args"] = list(args)
				return _ok(args, **kwargs)

			with patch("atlas.atlas._ssh.transport.KNOWN_HOSTS_PATH", fake_path):
				with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
					forget_host("[2400:6180:100:d0:0:1:517f:8002]")
			self.assertEqual(captured["args"][2], "2400:6180:100:d0:0:1:517f:8002")

	def test_swallows_missing_ssh_keygen(self) -> None:
		with tempfile.TemporaryDirectory() as temp_directory:
			fake_path = Path(temp_directory) / "known_hosts"
			fake_path.write_text("")
			with patch("atlas.atlas._ssh.transport.KNOWN_HOSTS_PATH", fake_path):
				with patch(
					"atlas.atlas._ssh.transport.subprocess.run",
					side_effect=FileNotFoundError(),
				):
					forget_host("10.0.0.1")  # must not raise
