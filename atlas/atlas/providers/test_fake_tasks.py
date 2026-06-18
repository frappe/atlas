"""Unit tests for the Fake provider's Task seam (no-SSH, result synthesis, faults).

These prove the second seam — `run_task` on a Fake-backed Server succeeds (or
fails on demand) without ever shelling out to `ssh` — and that real Virtual
Machine controller methods route through it.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.fake_tasks import (
	_fake_stdout,
	_parse_script_list,
	is_fake_server,
	run_fake_task,
)
from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result
from atlas.tests import fixtures


class _FakeServerCase(IntegrationTestCase):
	"""Shared setup: a Fake provider, a Fake-backed Active server, an image."""

	def setUp(self) -> None:
		self.provider = fixtures.make_provider_row("fake-test-provider", provider_type="Fake")
		fixtures.set_atlas_settings(self.provider)
		self.server = fixtures.make_server(
			self.provider,
			title="fake-test-server",
			status="Active",
			ipv4_address="203.0.113.10",
			ipv6_address="2001:db8:abcd::1",
			ipv6_prefix="2001:db8:abcd::/64",
			ipv6_virtual_machine_range="2001:db8:abcd::/124",
		)
		self.image = fixtures.make_image("fake-test-image")


class TestIsFakeServer(_FakeServerCase):
	def test_true_for_fake_backed_server(self) -> None:
		self.assertTrue(is_fake_server(self.server.name))

	def test_false_for_real_provider(self) -> None:
		real = fixtures.make_provider_row("do-test-provider", provider_type="DigitalOcean")
		server = fixtures.make_server(real, title="do-test-server", status="Active")
		self.assertFalse(is_fake_server(server.name))

	def test_false_for_missing_or_none(self) -> None:
		self.assertFalse(is_fake_server(None))
		self.assertFalse(is_fake_server("does-not-exist"))


class TestRunFakeTask(_FakeServerCase):
	def test_run_task_succeeds_without_ssh(self) -> None:
		# The whole point: a Task on a Fake server never shells out.
		with patch("atlas.atlas._ssh.transport.subprocess.run") as subprocess_run:
			task = run_task(
				server=self.server.name,
				script="start-vm.py",
				variables={"VIRTUAL_MACHINE_NAME": "x"},
			)
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.exit_code, 0)
		subprocess_run.assert_not_called()

	def test_run_fake_task_marks_success(self) -> None:
		task = run_fake_task(self.server.name, "stop-vm.py", {"VIRTUAL_MACHINE_NAME": "x"}, None)
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.server, self.server.name)
		self.assertEqual(task.script, "stop-vm.py")

	def test_records_virtual_machine_link(self) -> None:
		with patch("frappe.enqueue"):
			vm = fixtures.make_virtual_machine(self.server, self.image, title="fake-link-vm")
		task = run_fake_task(self.server.name, "stop-vm.py", {}, vm.name)
		self.assertEqual(task.virtual_machine, vm.name)


class TestFakeResultSynthesis(IntegrationTestCase):
	"""The four scripts whose controllers parse a result must get a valid one."""

	def test_bootstrap_result_round_trips(self) -> None:
		result = parse_result(_fake_stdout("bootstrap-server.py", {}))
		self.assertEqual(result["architecture"], "x86_64")
		self.assertIn("firecracker_version", result)
		self.assertIn("kernel_version", result)

	def test_snapshot_result_round_trips(self) -> None:
		result = parse_result(_fake_stdout("snapshot-vm.py", {"DISK_GB": "8"}))
		self.assertEqual(result["size_bytes"], 8 * 1024 * 1024 * 1024)
		self.assertEqual(result["data_size_bytes"], 0)

	def test_snapshot_result_reports_data_disk_when_present(self) -> None:
		result = parse_result(
			_fake_stdout("snapshot-vm.py", {"DISK_GB": "8", "DATA_SNAPSHOT_ROOTFS_PATH": "/dev/x"})
		)
		self.assertGreater(result["data_size_bytes"], 0)

	def test_snapshot_stop_result_round_trips(self) -> None:
		result = parse_result(_fake_stdout("snapshot-stop-vm.py", {}))
		self.assertTrue(result["memory_snapshot"])

	def test_warm_snapshot_result_round_trips(self) -> None:
		result = parse_result(_fake_stdout("warm-snapshot-vm.py", {"DISK_GB": "4"}))
		self.assertIn("memory_bytes", result)
		# host_signature is itself a JSON string the controller stores verbatim.
		self.assertIn("firecracker", result["host_signature"])

	def test_unparsed_script_emits_plain_ok(self) -> None:
		self.assertEqual(_fake_stdout("start-vm.py", {}), "ok\n")


class TestFaultInjection(_FakeServerCase):
	def test_flag_failure_raises_and_marks_failure(self) -> None:
		frappe.flags.fake_fail = {"script": "provision-vm.py", "reason": "boom"}
		try:
			with self.assertRaises(frappe.ValidationError):
				run_task(server=self.server.name, script="provision-vm.py", variables={})
		finally:
			frappe.flags.fake_fail = None
		# The Task row is left as Failure (the outcome contract).
		task = frappe.get_last_doc("Task", filters={"server": self.server.name})
		self.assertEqual(task.status, "Failure")

	def test_flag_failure_only_matches_named_script(self) -> None:
		frappe.flags.fake_fail = {"script": "provision-vm.py"}
		try:
			task = run_task(server=self.server.name, script="start-vm.py", variables={})
		finally:
			frappe.flags.fake_fail = None
		self.assertEqual(task.status, "Success")

	def test_flag_failure_wildcard_fails_everything(self) -> None:
		frappe.flags.fake_fail = "*"
		try:
			with self.assertRaises(frappe.ValidationError):
				run_task(server=self.server.name, script="anything.py", variables={})
		finally:
			frappe.flags.fake_fail = None

	def test_configured_fail_scripts_on_provider(self) -> None:
		frappe.db.set_value("Provider", self.provider.name, "fail_scripts", "snapshot-vm.py")
		with self.assertRaises(frappe.ValidationError):
			run_task(server=self.server.name, script="snapshot-vm.py", variables={"DISK_GB": "4"})
		# A different script on the same server still succeeds.
		task = run_task(server=self.server.name, script="start-vm.py", variables={})
		self.assertEqual(task.status, "Success")
		frappe.db.set_value("Provider", self.provider.name, "fail_scripts", "")

	def test_parse_script_list_handles_commas_and_newlines(self) -> None:
		self.assertEqual(_parse_script_list("a.py, b.py\nc.py"), {"a.py", "b.py", "c.py"})
		self.assertEqual(_parse_script_list(None), set())
		self.assertEqual(_parse_script_list(""), set())


class TestVirtualMachineThroughFake(_FakeServerCase):
	"""A real VM controller method must succeed against a Fake server, no SSH."""

	def test_auto_provision_entrypoint_succeeds_without_ssh_key(self) -> None:
		"""The worker calls `auto_provision`, not `provision` directly. Regression
		for: a fake provision must not reach `connection_for_server` (which reads
		the SSH key off disk) — even when no key file exists. The guard in
		`run_task` has to fire first."""
		from atlas.atlas.doctype.virtual_machine.virtual_machine import auto_provision

		# Point the key path at a file that does NOT exist: if the fake guard ever
		# fell through to connection_for_server, get_ssh_key_from_disk would throw.
		frappe.db.set_single_value(
			"Atlas Settings", "ssh_private_key_path", "/nonexistent/atlas-no-such-key.pem"
		)
		with patch("frappe.enqueue"):
			vm = frappe.get_doc(
				{
					"doctype": "Virtual Machine",
					"title": "fake-auto-provision",
					"server": self.server.name,
					"image": self.image.name,
					"vcpus": 1,
					"memory_megabytes": 512,
					"disk_gigabytes": 4,
					"ssh_public_key": "ssh-ed25519 AAAA",
				}
			).insert(ignore_permissions=True)
		self.assertEqual(vm.status, "Pending")
		auto_provision(vm.name)  # the exact worker entrypoint
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_provision_then_terminate(self) -> None:
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "fake-vm-lifecycle",
				"server": self.server.name,
				"image": self.image.name,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 4,
				"ssh_public_key": "ssh-ed25519 AAAA",
			}
		)
		# Drive the lifecycle by hand: patch enqueue so after_insert's
		# auto_provision doesn't race us, and assert no SSH the whole way.
		with (
			patch("atlas.atlas._ssh.transport.subprocess.run") as subprocess_run,
			patch("frappe.enqueue"),
		):
			vm.insert(ignore_permissions=True)
			vm.provision()
			self.assertEqual(vm.status, "Running")
			vm.stop()
			self.assertEqual(vm.status, "Stopped")
			vm.terminate()
			self.assertEqual(vm.status, "Terminated")
			subprocess_run.assert_not_called()
