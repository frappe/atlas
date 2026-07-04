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
				script="phase1-probe",
				variables={},
			)

	def test_rejects_neither_server_nor_connection(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			run_task(script="phase1-probe", variables={})


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
				script="phase1-probe",
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
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"server": self.server.name,
				"script": "phase1-probe",
				"variables": json.dumps({"NAME": "hi"}),
				"status": "Pending",
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

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
		task = frappe.get_doc(
			{
				"doctype": "Task",
				"server": None,
				"script": "phase1-probe",
				"variables": json.dumps({}),
				"status": "Pending",
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

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
			frappe.db.set_single_value("Atlas Settings", "ssh_private_key_path", "", update_modified=False)
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
					script="phase1-probe",
					variables={},
				)
		self.assertIn("boom", str(raised.exception))
		task = frappe.get_last_doc(
			"Task",
			filters={"script": "phase1-probe", "status": "Failure"},
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
					script="phase1-probe",
					variables={},
				)
		self.assertIs(raised.exception, inner)


class TestSidecarUploads(IntegrationTestCase):
	def test_sync_image_uploads_sidecars_then_runs_atlas_verb(self) -> None:
		# sync-image is a python verb WITH a sidecar (it bakes atlas-network.service
		# into the image). The sidecar is scp'd to its fixed path; the verb itself
		# runs as `atlas sync-image` — its own file is NOT scp'd (the console script
		# is the entry).
		scp_destinations: list[str] = []
		ssh_commands: list[str] = []

		def capture(args, **kwargs):
			if args[0] == "scp":
				# scp args: ["scp", "-i", key, ...SSH_OPTIONS, local, user@host:remote]
				scp_destinations.append(args[-1])
			elif args[0] == "ssh":
				ssh_commands.append(args[-1])
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=CONNECTION,
				script="sync-image",
				variables={"IMAGE_NAME": "test-image"},
			)

		# The sidecar atlas-network.service is uploaded.
		self.assertTrue(
			any("atlas-network.service" in destination for destination in scp_destinations),
			f"sidecar not in {scp_destinations}",
		)
		# The verb's own file is never scp'd — only the sidecar is.
		self.assertFalse(any(destination.endswith("sync-image.py") for destination in scp_destinations))
		# The verb runs through the console script.
		self.assertTrue(any(command.strip().startswith("atlas sync-image") for command in ssh_commands))


class TestStagingPath(IntegrationTestCase):
	"""The staging path survives for shell verbs that aren't shipped durably — the
	e2e probes resolved from the test directory."""

	def test_staging_purges_the_legacy_staged_package(self) -> None:
		# Hosts bootstrapped before the durable-package cutover still carry a
		# per-Task staged lib at /tmp/atlas/lib; a stale copy there would shadow
		# the durable package. The staging preamble must remove it before every
		# staged Task. Use a shell e2e probe (resolved from the test dir, never
		# shipped durably) so the staging path runs — durable production verbs run
		# as `atlas <verb>` and skip it entirely.
		ssh_commands: list[str] = []

		def capture(args, **kwargs):
			if args[0] == "ssh":
				ssh_commands.append(args[-1])
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=CONNECTION,
				script="phase1-probe",
				variables={"NAME": "x"},
			)

		staging = next(command for command in ssh_commands if "mkdir -p" in command)
		self.assertIn(f"rm -rf {runner.STALE_STAGED_PACKAGE_DIRECTORY}", staging)
		# The purge must come before the mkdir that re-creates the staging dir.
		self.assertLess(staging.index("rm -rf"), staging.index("mkdir -p"))
		# A shell probe is scp'd as its file (keeps .sh) and run with bash -x.
		self.assertTrue(ssh_commands[-1].strip().startswith("env ") or "bash -x" in ssh_commands[-1])


class TestDurableScriptInvocation(IntegrationTestCase):
	"""A durably-installed Python verb (provision/start/stop/…) runs as
	`atlas <verb>` — no per-Task mkdir+scp, no interpreter path, just one run."""

	def test_python_verb_runs_as_atlas_console_script(self) -> None:
		ssh_commands: list[str] = []
		scp_count = 0

		def capture(args, **kwargs):
			nonlocal scp_count
			if args[0] == "ssh":
				ssh_commands.append(args[-1])
			elif args[0] == "scp":
				scp_count += 1
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=CONNECTION,
				script="start-vm",
				variables={"VIRTUAL_MACHINE_NAME": "uuid-1"},
			)

		# No script transfer, no staging mkdir, no per-Task venv guard. A single ssh
		# call: `atlas start-vm --flags`. The scp+mkdir round trip is gone and the
		# fail-fast moved to once-at-bootstrap (Server.cli_ready).
		self.assertEqual(scp_count, 0)
		self.assertFalse(any("mkdir -p" in command for command in ssh_commands))
		self.assertEqual(len(ssh_commands), 1)
		self.assertTrue(ssh_commands[-1].strip().startswith("atlas start-vm "))
		self.assertIn("--virtual-machine-name uuid-1", ssh_commands[-1])
		# Not the old interpreter+path form.
		self.assertNotIn("/var/lib/atlas/bin/start-vm.py", ssh_commands[-1])
		self.assertNotIn("python3", ssh_commands[-1])

	def test_bootstrap_server_takes_the_atlas_fast_path(self) -> None:
		# The former carve-out: bootstrap-server now runs as `atlas bootstrap-server`
		# (install.sh created the venv first), so it takes the same scp-free fast path
		# as every other python verb — no per-Task transfer, no python3-by-path.
		ssh_commands: list[str] = []
		scp_count = 0

		def capture(args, **kwargs):
			nonlocal scp_count
			if args[0] == "ssh":
				ssh_commands.append(args[-1])
			elif args[0] == "scp":
				scp_count += 1
			return _ok(args, **kwargs)

		with patch("atlas.atlas._ssh.transport.subprocess.run", side_effect=capture):
			run_task(
				connection=CONNECTION,
				script="bootstrap-server",
				variables={"FIRECRACKER_VERSION": "v1.16.0", "ARCHITECTURE": "x86_64"},
			)

		self.assertEqual(scp_count, 0)
		self.assertEqual(len(ssh_commands), 1)
		self.assertTrue(ssh_commands[-1].strip().startswith("atlas bootstrap-server "))
		self.assertNotIn("python3", ssh_commands[-1])
		self.assertNotIn("PYTHONPATH", ssh_commands[-1])


class TestRemoteCommand(IntegrationTestCase):
	"""The verb-kind dispatch in runner._remote_command — the heart of the cutover.
	A python verb (incl. bootstrap-server) runs as `atlas <verb> --flag value`; a
	shell verb keeps `env VAR=val bash -x <file>`."""

	def test_python_verb_builds_atlas_console_command(self) -> None:
		command = runner._remote_command(
			"snapshot-vm",
			None,
			{"VIRTUAL_MACHINE_NAME": "uuid-1", "SNAPSHOT_ROOTFS_PATH": "/dev/atlas/x"},
		)
		# The pip-installed console script on PATH, no interpreter path, no PYTHONPATH.
		self.assertTrue(command.startswith("atlas snapshot-vm "))
		self.assertIn("--virtual-machine-name uuid-1", command)
		self.assertIn("--snapshot-rootfs-path /dev/atlas/x", command)
		self.assertNotIn("bash -x", command)
		self.assertNotIn("python3", command)
		self.assertNotIn("PYTHONPATH", command)

	def test_bootstrap_runs_as_the_atlas_console_verb(self) -> None:
		# NO CARVE-OUT: install.sh creates the Atlas venv + `atlas` console script
		# over SSH BEFORE the bootstrap Task, so bootstrap-server runs as a normal
		# `atlas bootstrap-server` verb on the venv — not host python3 by path.
		command = runner._remote_command(
			"bootstrap-server",
			None,
			{"FIRECRACKER_VERSION": "v1.16.0", "ARCHITECTURE": "x86_64"},
		)
		self.assertTrue(command.startswith("atlas bootstrap-server "))
		self.assertIn("--firecracker-version v1.16.0", command)
		self.assertIn("--architecture x86_64", command)
		# Specifically NOT the old carve-out shape.
		self.assertNotIn("python3", command)
		self.assertNotIn("PYTHONPATH", command)

	def test_non_bootstrap_python_never_uses_an_interpreter_path(self) -> None:
		# Every OTHER python verb runs as `atlas <verb>`, never an interpreter+path.
		command = runner._remote_command("start-vm", None, {"VIRTUAL_MACHINE_NAME": "u"})
		self.assertTrue(command.startswith("atlas start-vm "))
		self.assertNotIn(" python3 ", f" {command} ")

	def test_python_verb_repeats_list_flags(self) -> None:
		# A list value becomes a repeated flag; a value with an internal space
		# stays one shell-quoted token (the cpu.max "<quota> <period>" case).
		command = runner._remote_command(
			"provision-vm",
			None,
			{"CGROUP_ARG": ["memory.max=1", "cpu.max=200000 100000"]},
		)
		self.assertIn("--cgroup-arg memory.max=1", command)
		self.assertIn("--cgroup-arg 'cpu.max=200000 100000'", command)

	def test_python_verb_drops_empty_optional(self) -> None:
		command = runner._remote_command(
			"provision-vm",
			None,
			{"VIRTUAL_MACHINE_NAME": "uuid-1", "SNAPSHOT_ROOTFS_PATH": ""},
		)
		self.assertIn("--virtual-machine-name uuid-1", command)
		self.assertNotIn("snapshot-rootfs-path", command)

	def test_shell_verb_keeps_bash_env_form(self) -> None:
		command = runner._remote_command(
			"reboot-server",
			"/tmp/atlas/reboot-server.sh",
			{},
		)
		self.assertIn("bash -x /tmp/atlas/reboot-server.sh", command)
		self.assertNotIn("python3", command)
		self.assertNotIn("atlas reboot-server", command)
