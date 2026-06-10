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
			for token in cgroup_args(vm.cpu_max_cores, vm.memory_megabytes, vm.disk_gigabytes)
			if not token.startswith("--")
		]
		expected_resource = [
			token for token in resource_limit_args(vm.disk_gigabytes) if not token.startswith("--")
		]
		self.assertEqual(variables["CGROUP_ARG"], expected_cgroup)
		self.assertEqual(variables["RESOURCE_ARG"], expected_resource)
		# Regression guard for the word-splitting bug: cpu.max's "<quota> <period>"
		# value keeps its internal space as a SINGLE list element. A list flag
		# preserves it end to end — no mapfile, no systemd ExecStart shattering it
		# into a stray positional the jailer rejects.
		cpu_max = next(value for value in variables["CGROUP_ARG"] if value.startswith("cpu.max="))
		self.assertIn(" ", cpu_max, "cpu.max must keep its '<quota> <period>' space as one token")

	def test_cpu_max_cores_defaults_to_vcpus(self) -> None:
		# A caller who sets only vcpus (operator desk path, bootstrap, direct API)
		# gets whole-core bandwidth: cpu_max_cores defaults to vcpus.
		vm = _new_vm(vcpus=2)
		self.assertEqual(vm.cpu_max_cores, 2.0)
		# The provision cgroup cpu.max then reflects 2 cores.
		variables = vm._provision_variables()
		cpu_max = next(v for v in variables["CGROUP_ARG"] if v.startswith("cpu.max="))
		self.assertEqual(cpu_max, "cpu.max=200000 100000")

	def test_fractional_cpu_max_cores_in_provision_cgroup(self) -> None:
		# A 1/16-vCPU machine: one guest thread (vcpus=1), host-throttled to
		# 6.25% of a core. The guest boots on vcpu_count=1 (VCPUS), and the cgroup
		# cpu.max carries the fractional quota.
		vm = _new_vm(vcpus=1, cpu_max_cores=0.0625)
		self.assertEqual(vm.cpu_max_cores, 0.0625)
		variables = vm._provision_variables()
		self.assertEqual(variables["VCPUS"], "1", "guest still boots one vcpu thread")
		cpu_max = next(v for v in variables["CGROUP_ARG"] if v.startswith("cpu.max="))
		self.assertEqual(cpu_max, "cpu.max=6250 100000")

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
			task = frappe.get_doc(
				{
					"doctype": "Task",
					"server": kwargs["server"],
					"virtual_machine": kwargs.get("virtual_machine"),
					"script": kwargs["script"],
					"status": "Pending",
					"triggered_by": "Administrator",
				}
			)
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
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"server": _ensure_test_server(),
				"image": _ensure_test_image(),
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 2,
				"ssh_public_key": "ssh-ed25519 AAAA",
			}
		)
		vm.status = None
		vm.set_status_default()
		self.assertEqual(vm.status, "Pending")

	def test_set_status_default_keeps_existing(self) -> None:
		# `set_status_default` is a no-op when status is already populated.
		# Construct an in-memory VM and exercise the helper directly.
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"server": _ensure_test_server(),
				"image": _ensure_test_image(),
				"status": "Stopped",
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 2,
				"ssh_public_key": "ssh-ed25519 AAAA",
			}
		)
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
		task = fake_task(
			name="task-snap-1", stdout='+ stat\nATLAS_RESULT={"size_bytes": 4294967296}\nSnapshotted.'
		)

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

	def test_live_snapshot_from_running_creates_available_row(self) -> None:
		# live=True relaxes the Stopped requirement: a Running VM is snapshotted in
		# place (crash-consistent). The row still lands Available like a clean one.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		task = fake_task(name="task-live-snap", stdout='ATLAS_RESULT={"size_bytes": 4294967296}')
		with patch.object(module, "run_task", return_value=task) as mocked:
			snapshot_name = vm.snapshot("live one", live=True)
		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-vm.py")
		snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
		self.assertEqual(snapshot.status, "Available")

	def test_live_snapshot_accepts_stringy_true(self) -> None:
		# frm.call / REST may send live as the string "true"; it must coerce to bool.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		task = fake_task(name="task-live-snap-2", stdout='ATLAS_RESULT={"size_bytes": 1}')
		with patch.object(module, "run_task", return_value=task):
			snapshot_name = vm.snapshot("live two", live="true")
		self.assertEqual(frappe.db.get_value("Virtual Machine Snapshot", snapshot_name, "status"), "Available")

	def test_regenerate_host_keys_runs_script_when_stopped(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-regen")) as mocked:
			vm.regenerate_host_keys()
		self.assertEqual(mocked.call_args.kwargs["script"], "regenerate-host-keys-vm.py")
		self.assertEqual(mocked.call_args.kwargs["variables"]["VIRTUAL_MACHINE_NAME"], vm.name)

	def test_regenerate_host_keys_rejects_when_not_stopped(self) -> None:
		# Mounting the rootfs to rewrite keys needs the guest's fs unmounted.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.regenerate_host_keys()
		self.assertIn("Stop the VM before regenerating host keys", str(raised.exception))
		mocked.assert_not_called()

	def test_live_snapshot_rejects_when_pending(self) -> None:
		# Live needs a Running/Paused VM — there is no live disk to snapshot from
		# Pending/Failed/Terminated.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # Pending
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.snapshot("nope", live=True)
		self.assertIn("Live snapshot needs a Running or Paused VM", str(raised.exception))
		mocked.assert_not_called()

	def test_terminate_deletes_snapshot_rows(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(
			module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1}')
		):
			snapshot_name = vm.snapshot("doomed")
		self.assertTrue(frappe.db.exists("Virtual Machine Snapshot", snapshot_name))

		# Terminate cascades the snapshot rows. Each snapshot's on_trash runs
		# delete-snapshot-vm.py to lvremove its snapshot LV — snapshot LVs live in
		# the thin pool, OUTSIDE the VM directory terminate-vm.py rm -rf'd, so they
		# must be removed explicitly (no Terminated short-circuit). on_trash uses
		# the snapshot module's run_task, so patch that one too.
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as snapshot_module

		with (
			patch.object(module, "run_task", return_value=fake_task(name="task-term")),
			patch.object(
				snapshot_module, "run_task", return_value=fake_task(name="task-snap-del")
			) as mocked_snapshot,
		):
			vm.terminate()
		self.assertFalse(frappe.db.exists("Virtual Machine Snapshot", snapshot_name))
		# The snapshot LV was removed via the per-snapshot delete script.
		self.assertEqual(mocked_snapshot.call_args.kwargs["script"], "delete-snapshot-vm.py")

	def test_terminate_preserves_the_golden_bench_snapshot(self) -> None:
		# The golden snapshot is the durable artifact self-serve sites clone from;
		# terminating its build VM (bake scratch) must NOT delete it, or the row stays
		# Available while its LV is gone and the next clone fails late (snapshot LV not
		# found). See _delete_snapshots' golden skip.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(
			module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1}')
		):
			snapshot_name = vm.snapshot("golden")
		frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", snapshot_name)

		with patch.object(module, "run_task", return_value=fake_task(name="task-term")):
			vm.terminate()
		# The referenced golden survives the build VM's termination.
		self.assertTrue(frappe.db.exists("Virtual Machine Snapshot", snapshot_name))

	def test_parse_size_bytes(self) -> None:
		from atlas.atlas.task_results import parse_result

		self.assertEqual(parse_result('+ cmd\nATLAS_RESULT={"size_bytes": 512}\ndone')["size_bytes"], 512)

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

	# --- data disk (the root disk's peer) ---

	def test_provision_variables_omit_data_disk_when_none(self) -> None:
		# Default VM has no data disk (data_disk_gigabytes=0) → no DATA_* vars, so
		# provision-vm.py's defaults leave the second drive off entirely.
		vm = _new_vm()
		variables = vm._provision_variables()
		self.assertNotIn("DATA_DISK_GB", variables)
		self.assertNotIn("DATA_DISK_MOUNT_AT", variables)

	def test_provision_variables_carry_data_disk(self) -> None:
		vm = _new_vm(data_disk_gigabytes=3, data_disk_format_and_mount=1, data_disk_mount_point="/home")
		variables = vm._provision_variables()
		self.assertEqual(variables["DATA_DISK_GB"], "3")
		self.assertEqual(variables["DATA_DISK_FORMAT"], "1")
		self.assertEqual(variables["DATA_DISK_MOUNT_AT"], "/home")

	def test_provision_variables_data_disk_unformatted_has_no_mount(self) -> None:
		# format-and-mount off: raw block device, so no mount point is sent (the
		# runner drops the empty value) and DATA_DISK_FORMAT is "0".
		vm = _new_vm(data_disk_gigabytes=3, data_disk_format_and_mount=0)
		variables = vm._provision_variables()
		self.assertEqual(variables["DATA_DISK_FORMAT"], "0")
		self.assertEqual(variables["DATA_DISK_MOUNT_AT"], "")

	def test_resize_grows_data_disk(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm(data_disk_gigabytes=2)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-resize")) as mocked:
			vm.resize(data_disk_gigabytes=5)
		vm.reload()
		self.assertEqual(vm.data_disk_gigabytes, 5)
		self.assertEqual(mocked.call_args.kwargs["variables"]["DATA_DISK_GB"], "5")

	def test_resize_data_disk_rejects_shrink(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm(data_disk_gigabytes=4)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.resize(data_disk_gigabytes=2)
		self.assertIn("Data disk can only grow", str(raised.exception))
		mocked.assert_not_called()

	def test_resize_rejects_adding_data_disk_to_vm_without_one(self) -> None:
		# 0 -> N is out of scope for resize (it would need a new Firecracker drive
		# + fstab line): recreate the VM instead.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # no data disk
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.resize(data_disk_gigabytes=4)
		self.assertIn("no data disk", str(raised.exception))
		mocked.assert_not_called()

	def test_snapshot_persists_data_disk_fields(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm(data_disk_gigabytes=2, data_disk_format_and_mount=1, data_disk_mount_point="/home")
		vm.db_set("status", "Stopped")
		vm.reload()
		task = fake_task(
			name="task-snap-data",
			stdout='ATLAS_RESULT={"size_bytes": 4294967296, "data_size_bytes": 2147483648}',
		)
		with patch.object(module, "run_task", return_value=task) as mocked:
			snapshot_name = vm.snapshot("with-data")

		# The data half is snapshotted under the SAME snapshot UUID.
		self.assertEqual(
			mocked.call_args.kwargs["variables"]["DATA_SNAPSHOT_ROOTFS_PATH"],
			f"/dev/atlas/atlas-datasnap-{snapshot_name}",
		)
		snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
		self.assertEqual(snapshot.data_disk_gigabytes, 2)
		self.assertEqual(snapshot.data_disk_mount_point, "/home")
		self.assertEqual(snapshot.data_size_bytes, 2147483648)
		self.assertEqual(snapshot.data_rootfs_path, f"/dev/atlas/atlas-datasnap-{snapshot_name}")

	def test_snapshot_without_data_disk_has_no_data_snapshot(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # no data disk
		vm.db_set("status", "Stopped")
		vm.reload()
		task = fake_task(name="task-snap-nodata", stdout='ATLAS_RESULT={"size_bytes": 1024}')
		with patch.object(module, "run_task", return_value=task) as mocked:
			snapshot_name = vm.snapshot("no-data")
		self.assertNotIn("DATA_SNAPSHOT_ROOTFS_PATH", mocked.call_args.kwargs["variables"])
		snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
		self.assertFalse(snapshot.data_rootfs_path)
		self.assertEqual(snapshot.data_size_bytes, 0)
