"""Unit coverage for the ad-hoc SSH fan-out engine (`atlas.atlas.ssh_console`).

These are the spec's "unit-covered logic" half: classification of a command's
outcome, the Server/guest connection dispatch, and the fan-out's streaming and
failure-isolation contract — all in milliseconds with no host. The host fact
(a real command over a real droplet/guest) rides the e2e `run_task` module.
"""

import subprocess
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from atlas.atlas import ssh_console
from atlas.atlas.ssh import Connection

CONNECTION = Connection(host="10.0.0.5", ssh_private_key="KEY")


class TestRunOnTarget(IntegrationTestCase):
	def setUp(self) -> None:
		self.target = ssh_console.Target(kind="Server", name="srv-a")

	def _run(self, run_ssh_side_effect):
		with (
			patch.object(ssh_console, "connection_for_target", return_value=CONNECTION),
			patch.object(ssh_console, "run_ssh", side_effect=run_ssh_side_effect) as run_ssh,
		):
			result = ssh_console.run_on_target(self.target, "uname -a", timeout_seconds=5)
		return result, run_ssh

	def test_zero_exit_is_success(self) -> None:
		result, _ = self._run(lambda *a, **k: ("Linux\n", "", 0))
		self.assertEqual(result.status, ssh_console.SUCCESS)
		self.assertEqual(result.exit_code, 0)
		self.assertEqual(result.stdout, "Linux\n")
		self.assertEqual(result.target_kind, "Server")
		self.assertEqual(result.target_name, "srv-a")

	def test_nonzero_exit_is_failure_not_raise(self) -> None:
		result, _ = self._run(lambda *a, **k: ("", "boom\n", 7))
		self.assertEqual(result.status, ssh_console.FAILURE)
		self.assertEqual(result.exit_code, 7)
		self.assertEqual(result.stderr, "boom\n")

	def test_timeout_is_unreachable_not_raise(self) -> None:
		def raise_timeout(*args, **kwargs):
			raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

		result, _ = self._run(raise_timeout)
		self.assertEqual(result.status, ssh_console.UNREACHABLE)
		self.assertIsNone(result.exit_code)
		self.assertIn("timed out", result.stderr)

	def test_transport_error_is_unreachable_not_raise(self) -> None:
		def raise_error(*args, **kwargs):
			raise ValueError("no ipv4_address")

		result, _ = self._run(raise_error)
		self.assertEqual(result.status, ssh_console.UNREACHABLE)
		self.assertIsNone(result.exit_code)
		self.assertIn("no ipv4_address", result.stderr)


class TestConnectionDispatch(IntegrationTestCase):
	def test_server_uses_connection_for_server(self) -> None:
		target = ssh_console.Target(kind="Server", name="srv-a")
		with (
			patch.object(ssh_console, "frappe") as fake_frappe,
			patch.object(ssh_console, "connection_for_server", return_value=CONNECTION) as host,
			patch.object(ssh_console, "connection_for_guest") as guest,
		):
			fake_frappe.get_doc.return_value = object()
			self.assertIs(ssh_console.connection_for_target(target), CONNECTION)
		host.assert_called_once()
		guest.assert_not_called()

	def test_guest_uses_connection_for_guest(self) -> None:
		target = ssh_console.Target(kind="Virtual Machine", name="vm-a")
		with (
			patch.object(ssh_console, "frappe") as fake_frappe,
			patch.object(ssh_console, "connection_for_server") as host,
			patch.object(ssh_console, "connection_for_guest", return_value=CONNECTION) as guest,
		):
			fake_frappe.get_doc.return_value = object()
			self.assertIs(ssh_console.connection_for_target(target), CONNECTION)
		guest.assert_called_once()
		host.assert_not_called()

	def test_unknown_kind_rejected(self) -> None:
		import frappe

		with self.assertRaises(frappe.exceptions.ValidationError):
			ssh_console.Target(kind="Printer", name="p1")


class TestFanOut(IntegrationTestCase):
	def setUp(self) -> None:
		self.targets = [
			ssh_console.Target(kind="Server", name="srv-a"),
			ssh_console.Target(kind="Server", name="srv-b"),
			ssh_console.Target(kind="Virtual Machine", name="vm-c"),
		]

	def _result(self, target, status=ssh_console.SUCCESS):
		return ssh_console.CommandResult(
			target_kind=target.kind,
			target_name=target.name,
			status=status,
			stdout="ok",
			stderr="",
			exit_code=0,
			duration_milliseconds=1,
		)

	def test_on_result_called_once_per_target_in_order(self) -> None:
		seen = []
		with patch.object(ssh_console, "run_on_target", side_effect=lambda t, c, **k: self._result(t)):
			results = ssh_console.run_fan_out(
				self.targets, "echo hi", on_result=lambda r: seen.append(r.target_name)
			)
		self.assertEqual(seen, ["srv-a", "srv-b", "vm-c"])
		self.assertEqual(len(results), 3)

	def test_sink_exception_does_not_abort_remaining_targets(self) -> None:
		def angry_sink(_result):
			raise RuntimeError("sink down")

		with patch.object(ssh_console, "run_on_target", side_effect=lambda t, c, **k: self._result(t)):
			results = ssh_console.run_fan_out(self.targets, "echo hi", on_result=angry_sink)
		# Every target still ran despite the sink raising on each.
		self.assertEqual([r.target_name for r in results], ["srv-a", "srv-b", "vm-c"])

	def test_per_target_failure_does_not_propagate(self) -> None:
		def mixed(target, command, **kwargs):
			status = ssh_console.FAILURE if target.name == "srv-b" else ssh_console.SUCCESS
			return self._result(target, status=status)

		with patch.object(ssh_console, "run_on_target", side_effect=mixed):
			results = ssh_console.run_fan_out(self.targets, "echo hi", on_result=lambda r: None)
		statuses = {r.target_name: r.status for r in results}
		self.assertEqual(statuses["srv-b"], ssh_console.FAILURE)
		self.assertEqual(statuses["srv-a"], ssh_console.SUCCESS)
