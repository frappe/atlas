from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _ensure_test_server() -> str:
	provider = make_provider("vm-test-provider")
	server = make_server(
		provider,
		"vm-test-server",
		ipv4_address="10.0.0.99",
		ipv6_address="2001:db8:1::1",
		ipv6_prefix="2001:db8:1::/64",
		ipv6_virtual_machine_range="2001:db8:1::/124",
		status="Active",
	)
	return server.name


def _ensure_test_image() -> str:
	return make_image("vm-test-image-2").name


def _new_vm(**overrides) -> "frappe.model.document.Document":
	return make_virtual_machine(_ensure_test_server(), _ensure_test_image(), **overrides)


class TestVirtualMachine(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		# Clear VMs from prior tests so the /124 IPv6 range has capacity.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_before_insert_sets_uuid_mac_tap_ipv6(self) -> None:
		vm = _new_vm()
		# Frappe-validated UUID: 36 chars with 4 dashes
		self.assertEqual(len(vm.name), 36)
		self.assertEqual(vm.name.count("-"), 4)
		self.assertTrue(vm.mac_address.startswith("06:00:"))
		self.assertTrue(vm.tap_device.startswith("atlas-"))
		self.assertEqual(len(vm.tap_device), 15)
		self.assertTrue(vm.ipv6_address.startswith("2001:db8:1::"))
		self.assertEqual(vm.status, "Pending")

	def test_immutable_fields_raise(self) -> None:
		vm = _new_vm()
		vm.vcpus = 4
		with self.assertRaises(frappe.ValidationError):
			vm.save(ignore_permissions=True)

	def test_provision_runs_when_image_present(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		task = fake_task(name="task-prov-1")

		with patch.object(module, "run_task", return_value=task) as mocked:
			vm.provision()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertIsNotNone(vm.last_started)
		# One Task per VM creation: provision-vm.py's step 0 is the image probe.
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "provision-vm.py")

	def test_provision_variables_carry_jail_parameters(self) -> None:
		from atlas.atlas.networking import (
			cgroup_args,
			derive_netns,
			derive_uid,
			derive_veth_pair,
			resource_limit_args,
		)

		vm = _new_vm()
		variables = vm._provision_variables()
		host_veth, namespace_veth = derive_veth_pair(vm.name)
		self.assertEqual(variables["ATLAS_FC_UID"], str(derive_uid(vm.name)))
		self.assertEqual(variables["ATLAS_NETNS"], derive_netns(vm.name))
		self.assertEqual(variables["HOST_VETH"], host_veth)
		self.assertEqual(variables["NAMESPACE_VETH"], namespace_veth)
		# cgroup/resource limits are values-only LISTS (the runner renders each as
		# a repeatable --cgroup-arg / --resource-arg flag; provision-vm.py prefixes
		# each with --cgroup / --resource-limit when it builds the launcher). The
		# interleaved "--cgroup" flag tokens from networking.cgroup_args are
		# stripped — every element is a bare value.
		expected_cgroup = [
			token
			for token in cgroup_args(vm.vcpus, vm.memory_megabytes, vm.disk_gigabytes)
			if not token.startswith("--")
		]
		expected_resource = [
			token
			for token in resource_limit_args(vm.disk_gigabytes)
			if not token.startswith("--")
		]
		self.assertEqual(variables["CGROUP_ARG"], expected_cgroup)
		self.assertEqual(variables["RESOURCE_ARG"], expected_resource)
		# Regression guard for the word-splitting bug: cpu.max's "<quota> <period>"
		# value keeps its internal space as a SINGLE list element. A list flag
		# preserves it end to end — no mapfile, no systemd ExecStart shattering it
		# into a stray positional the jailer rejects.
		cpu_max = next(value for value in variables["CGROUP_ARG"] if value.startswith("cpu.max="))
		self.assertIn(" ", cpu_max, "cpu.max must keep its '<quota> <period>' space as one token")

	def test_provision_failure_flips_status_to_failed(self) -> None:
		"""On failure the Task is saved with `Failure`; the Task controller
		hook then flips the linked VM's status to `Failed` so the form makes
		the failed attempt visible. The operator re-clicks Provision (scripts
		are idempotent) to retry."""
		from atlas.atlas.doctype.task.task import Task
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()

		def fake_run_task(**kwargs):
			# Mimic the real `run_task`: insert a Task row, mark it Failure,
			# raise. The on_update hook on the Task then propagates Failed
			# to the linked VM.
			task = frappe.get_doc({
				"doctype": "Task",
				"server": kwargs["server"],
				"virtual_machine": kwargs.get("virtual_machine"),
				"script": kwargs["script"],
				"status": "Pending",
				"triggered_by": "Administrator",
			})
			import json as _json
			task.variables = _json.dumps(kwargs.get("variables") or {})
			task.insert(ignore_permissions=True)
			task.status = "Failure"
			task.stderr = "provision broke"
			task.exit_code = 1
			task.save(ignore_permissions=True)
			raise frappe.ValidationError("provision broke")

		with patch.object(module, "run_task", side_effect=fake_run_task):
			with self.assertRaises(frappe.ValidationError):
				vm.provision()
		vm.reload()
		self.assertEqual(vm.status, "Failed")

	def test_provision_rejects_from_running(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.provision()
		self.assertIn("Cannot provision from Running", str(raised.exception))
		mocked.assert_not_called()

	def test_validate_skips_when_no_before_save(self) -> None:
		# Defensive branch: a non-new VM whose `_doc_before_save` was cleared
		# should early-return from validate without comparing immutables.
		vm = _new_vm()
		vm._doc_before_save = None
		vm.vcpus = 99
		# Directly invoke validate; should not throw.
		vm.validate()

	def test_set_status_default_assigns_pending_when_empty(self) -> None:
		# Frappe's JSON default pre-populates status, so we have to clear it
		# in-memory to exercise the assignment branch.
		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"server": _ensure_test_server(),
			"image": _ensure_test_image(),
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 2,
			"ssh_public_key": "ssh-ed25519 AAAA",
		})
		vm.status = None
		vm.set_status_default()
		self.assertEqual(vm.status, "Pending")

	def test_set_status_default_keeps_existing(self) -> None:
		# `set_status_default` is a no-op when status is already populated.
		# Construct an in-memory VM and exercise the helper directly.
		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"server": _ensure_test_server(),
			"image": _ensure_test_image(),
			"status": "Stopped",
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 2,
			"ssh_public_key": "ssh-ed25519 AAAA",
		})
		vm.set_status_default()
		self.assertEqual(vm.status, "Stopped")

	def test_set_ipv6_address_keeps_existing(self) -> None:
		vm = make_virtual_machine(
			_ensure_test_server(),
			_ensure_test_image(),
			ipv6_address="2001:db8:1::abcd",
		)
		self.assertEqual(vm.ipv6_address, "2001:db8:1::abcd")

	def test_set_mac_address_keeps_existing(self) -> None:
		vm = make_virtual_machine(
			_ensure_test_server(),
			_ensure_test_image(),
			mac_address="06:00:11:22:33:44",
		)
		self.assertEqual(vm.mac_address, "06:00:11:22:33:44")

	def test_set_tap_device_keeps_existing(self) -> None:
		vm = make_virtual_machine(
			_ensure_test_server(),
			_ensure_test_image(),
			tap_device="atlas-aabbccdd1",
		)
		self.assertEqual(vm.tap_device, "atlas-aabbccdd1")

	def test_after_insert_enqueues_auto_provision(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		with patch.object(module.frappe, "enqueue") as enqueue:
			vm = _new_vm()
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(
			kwargs["virtual_machine_name"],
			vm.name,
		)

	def test_auto_provision_is_noop_when_not_pending(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		with patch.object(module, "run_task") as mocked:
			module.auto_provision(vm.name)
		mocked.assert_not_called()

	def test_auto_provision_calls_provision_when_pending(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		task = fake_task(name="task-auto-1")
		with patch.object(module, "run_task", return_value=task):
			module.auto_provision(vm.name)
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_snapshot_from_stopped_creates_available_row(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		task = fake_task(name="task-snap-1", stdout='+ stat\nATLAS_RESULT={"size_bytes": 4294967296}\nSnapshotted.')

		with patch.object(module, "run_task", return_value=task) as mocked:
			snapshot_name = vm.snapshot("nightly")

		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-vm.py")
		snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
		self.assertEqual(snapshot.status, "Available")
		self.assertEqual(snapshot.virtual_machine, vm.name)
		self.assertEqual(snapshot.server, vm.server)
		self.assertEqual(snapshot.source_image, vm.image)
		self.assertEqual(snapshot.disk_gigabytes, vm.disk_gigabytes)
		self.assertEqual(snapshot.size_bytes, 4294967296)
		# The snapshot is a thin snapshot LV named by the snapshot's own UUID
		# (it lives in the pool, independent of the VM), so its device path is
		# /dev/atlas/atlas-snap-<snapshot-uuid> — keyed by the snapshot, not the VM.
		self.assertEqual(snapshot.rootfs_path, f"/dev/atlas/atlas-snap-{snapshot.name}")

	def test_snapshot_rejects_when_not_stopped(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.snapshot("nope")
		self.assertIn("Stop the VM before snapshotting", str(raised.exception))
		mocked.assert_not_called()

	def test_terminate_deletes_snapshot_rows(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1}')):
			snapshot_name = vm.snapshot("doomed")
		self.assertTrue(frappe.db.exists("Virtual Machine Snapshot", snapshot_name))

		# Terminate cascades the snapshot rows. Each snapshot's on_trash runs
		# delete-snapshot-vm.py to lvremove its snapshot LV — snapshot LVs live in
		# the thin pool, OUTSIDE the VM directory terminate-vm.py rm -rf'd, so they
		# must be removed explicitly (no Terminated short-circuit). on_trash uses
		# the snapshot module's run_task, so patch that one too.
		from atlas.atlas.doctype.virtual_machine_snapshot import (
			virtual_machine_snapshot as snapshot_module,
		)

		with patch.object(module, "run_task", return_value=fake_task(name="task-term")), patch.object(
			snapshot_module, "run_task", return_value=fake_task(name="task-snap-del")
		) as mocked_snapshot:
			vm.terminate()
		self.assertFalse(frappe.db.exists("Virtual Machine Snapshot", snapshot_name))
		# The snapshot LV was removed via the per-snapshot delete script.
		self.assertEqual(mocked_snapshot.call_args.kwargs["script"], "delete-snapshot-vm.py")

	def test_parse_size_bytes(self) -> None:
		from atlas.atlas.task_results import parse_result

		self.assertEqual(
			parse_result('+ cmd\nATLAS_RESULT={"size_bytes": 512}\ndone')["size_bytes"], 512
		)

	def test_title_is_immutable(self) -> None:
		vm = _new_vm()
		vm.title = "renamed"
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.save(ignore_permissions=True)
		self.assertIn("title is immutable", str(raised.exception))

	def test_ssh_public_key_is_immutable(self) -> None:
		vm = _new_vm()
		vm.ssh_public_key = "ssh-ed25519 NEW"
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.save(ignore_permissions=True)
		self.assertIn("ssh_public_key is immutable", str(raised.exception))
