from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.placement import NoResizeCapacityError
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _resize_server(**totals) -> str:
	"""A distinct Active server catalogued with the given capacity totals — the
	resize-gate tests need a MEASURED host so the gate has an effective budget to
	check against (the shared vm-test-server is memory/disk-uncatalogued on purpose).
	Also clears the memory floor so `effective` equals the stamped total exactly."""
	frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 0)
	provider = make_provider("vm-resize-provider")
	server = make_server(
		provider,
		"vm-resize-server",
		ipv4_address="10.0.0.77",
		ipv6_address="2001:db8:5::1",
		ipv6_prefix="2001:db8:5::/64",
		ipv6_virtual_machine_range="2001:db8:5::/120",
		status="Active",
	)
	stamped = {"vcpus_total": 0, "memory_megabytes_total": 0, "pool_disk_gigabytes_total": 0, **totals}
	for field, value in stamped.items():
		server.db_set(field, value)
	return server.name


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


def _ensure_active_root_domain(domain: str = "blr1.frappe.dev") -> str:
	"""A single active Root Domain so `active_root_domain()` resolves. Provider types
	and region are set explicitly so the row inserts without depending on Settings."""
	if frappe.db.exists("Root Domain", domain):
		return domain
	frappe.get_doc(
		{
			"doctype": "Root Domain",
			"domain": domain,
			"region": "blr1",
			"is_active": 1,
			"dns_provider_type": "Route53",
			"tls_provider_type": "Let's Encrypt",
		}
	).insert(ignore_permissions=True)
	return domain


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
		self.assertEqual(mocked.call_args.kwargs["script"], "provision-vm")

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
			for token in cgroup_args(
				vm.cpu_max_cores,
				vm.memory_megabytes,
				vm.disk_gigabytes,
				vm.cpu_mode,
				vm.vcpus,
			)
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
		# A 1/16-vCPU machine under hard-cap mode: one guest thread (vcpus=1),
		# host-throttled to 6.25% of a core. The guest boots on vcpu_count=1
		# (VCPUS), and the cgroup cpu.max carries the fractional quota.
		vm = _new_vm(vcpus=1, cpu_max_cores=0.0625, cpu_mode="Hard cap")
		self.assertEqual(vm.cpu_max_cores, 0.0625)
		variables = vm._provision_variables()
		self.assertEqual(variables["VCPUS"], "1", "guest still boots one vcpu thread")
		cpu_max = next(v for v in variables["CGROUP_ARG"] if v.startswith("cpu.max="))
		self.assertEqual(cpu_max, "cpu.max=6250 100000")

	def test_cpu_mode_defaults_to_relaxed(self) -> None:
		# A caller who sets no cpu_mode (operator desk path, bootstrap, direct API)
		# gets the relaxed model: cpu.weight floor + a loose whole-vcpu burst
		# ceiling, so the VM bursts into spare host CPU when the host is idle.
		vm = _new_vm(vcpus=1, cpu_max_cores=0.0625)
		self.assertEqual(vm.cpu_mode, "Relaxed")
		values = vm._provision_variables()["CGROUP_ARG"]
		self.assertIn("cpu.weight=6", values)
		self.assertIn("cpu.max=100000 100000", values)

	def test_hard_cap_mode_emits_ceiling_and_no_weight(self) -> None:
		# Explicit hard-cap mode gives the original hard ceiling: cpu.max ==
		# cpu_max_cores, no cpu.weight — a 1/16 VM throttled even on an idle host.
		vm = _new_vm(vcpus=1, cpu_max_cores=0.0625, cpu_mode="Hard cap")
		self.assertEqual(vm.cpu_mode, "Hard cap")
		values = vm._provision_variables()["CGROUP_ARG"]
		self.assertIn("cpu.max=6250 100000", values)
		self.assertFalse(
			any(v.startswith("cpu.weight=") for v in values),
			"hard-cap mode emits no cpu.weight",
		)

	def test_relaxed_mode_emits_weight_and_loose_ceiling(self) -> None:
		# Relaxed mode: cpu_max_cores becomes the guaranteed share carried by
		# cpu.weight (proportional, 1/16 core -> ~6), and cpu.max is loosened to
		# the whole-vcpu burst ceiling so the VM bursts into idle host CPU.
		vm = _new_vm(vcpus=1, cpu_max_cores=0.0625, cpu_mode="Relaxed")
		self.assertEqual(vm.cpu_mode, "Relaxed")
		values = vm._provision_variables()["CGROUP_ARG"]
		self.assertIn("cpu.weight=6", values)
		# vcpus=1 -> a one-core ceiling, not the 6.25% hard wall.
		self.assertIn("cpu.max=100000 100000", values)

	def test_relaxed_mode_ceiling_tracks_vcpus(self) -> None:
		# The burst ceiling is vcpus whole cores: a 2-vCPU relaxed VM may burst to
		# two cores. Its guaranteed weight reflects the (here whole-core) share.
		vm = _new_vm(vcpus=2, cpu_max_cores=2, cpu_mode="Relaxed")
		values = vm._provision_variables()["CGROUP_ARG"]
		self.assertIn("cpu.weight=200", values)
		self.assertIn("cpu.max=200000 100000", values)

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
		# The insert also enqueues central_report.deliver events (vm.created,
		# vm.status_changed); single out the auto_provision enqueue and assert it
		# targets this VM, rather than assuming it is the only enqueue.
		auto_provision_calls = [
			call
			for call in enqueue.call_args_list
			if call.args and call.args[0].endswith(".auto_provision")
		]
		self.assertEqual(len(auto_provision_calls), 1)
		self.assertEqual(auto_provision_calls[0].kwargs["virtual_machine_name"], vm.name)

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

		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-vm")
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
		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-vm")
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
		self.assertEqual(
			frappe.db.get_value("Virtual Machine Snapshot", snapshot_name, "status"), "Available"
		)

	def test_regenerate_host_keys_runs_script_when_stopped(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-regen")) as mocked:
			vm.regenerate_host_keys()
		self.assertEqual(mocked.call_args.kwargs["script"], "regenerate-host-keys-vm")
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
		self.assertEqual(mocked_snapshot.call_args.kwargs["script"], "delete-snapshot-vm")

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

	def test_terminate_deprovisions_a_proxy(self) -> None:
		# A terminated proxy must drop out of the fleet: `is_proxy` clears and the
		# wildcard is re-published so the dead /128 stops answering in the round-robin.
		from atlas.atlas.doctype.tls_certificate import tls_certificate as cert_module
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		domain = _ensure_active_root_domain()
		cert = frappe.get_doc(
			{"doctype": "TLS Certificate", "root_domain": domain, "status": "Active"}
		).insert(ignore_permissions=True)

		vm = _new_vm(is_proxy=1)
		vm.db_set("status", "Stopped")
		vm.reload()

		with (
			patch.object(module, "run_task", return_value=fake_task(name="task-term")),
			# Spy the re-publish so no real Route53 call fires; assert it targets the
			# region's Active cert.
			patch.object(cert_module.TLSCertificate, "_publish_wildcard") as publish,
		):
			vm.terminate()

		self.assertFalse(frappe.db.get_value("Virtual Machine", vm.name, "is_proxy"))
		publish.assert_called_once()
		self.assertTrue(frappe.db.exists("TLS Certificate", cert.name))

	def test_terminate_deletes_a_subdomain_a_pilot_still_links(self) -> None:
		# A bench VM's Subdomain is linked-TO by the Pilot that fronts it
		# (`subdomain_doc`), so deleting it out from under the Pilot would trip Frappe's
		# link-integrity guard (LinkExistsError). This is the exact state Central's
		# terminate_server drives (run_doc_method → the VM's own terminate). Terminate
		# must clear the Pilot's link first, then delete the Subdomain — not 500.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		_ensure_active_root_domain()
		vm = _new_vm()
		subdomain = frappe.get_doc(
			{
				"doctype": "Subdomain",
				"subdomain": "linked-sub",
				"virtual_machine": vm.name,
				"address": "2001:db8:1::5",
			}
		).insert(ignore_permissions=True)
		# The attach path binds an existing VM without provisioning a new one, so the
		# Pilot lands with a subdomain_doc link but no heavy after_insert side effects.
		pilot = frappe.get_doc({"doctype": "Pilot", "subdomain": "linked-sub"})
		pilot.flags.attach_vm = vm.name
		pilot.insert(ignore_permissions=True)
		pilot.db_set("subdomain_doc", subdomain.name)

		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-term")):
			vm.terminate()  # must not raise LinkExistsError

		self.assertFalse(frappe.db.exists("Subdomain", subdomain.name))
		self.assertIsNone(frappe.db.get_value("Pilot", pilot.name, "subdomain_doc"))

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

	# --- memory snapshots (fast stop/start) ---

	def test_stop_plain_by_default(self) -> None:
		# Without the opt-in, stop is the plain unit stop — the default path.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # memory_snapshot_on_stop defaults OFF
		vm.db_set("status", "Running")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-stop")) as mocked:
			vm.stop()
		vm.reload()
		self.assertEqual(vm.status, "Stopped")
		self.assertEqual(mocked.call_args.kwargs["script"], "stop-vm")
		self.assertFalse(vm.has_memory_snapshot)
		# Cooperative shutdown by default: the guest gets a ctrl+alt+del (GRACEFUL=1)
		# so it syncs + unmounts before the unit is killed.
		self.assertEqual(mocked.call_args.kwargs["variables"]["GRACEFUL"], "1")

	def test_stop_forced_skips_graceful_shutdown(self) -> None:
		# graceful=False is the forced kill — no ctrl+alt+del, guest RAM discarded.
		# The migration cold-stop and any caller that throws the RAM away use this.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-stop")) as mocked:
			vm.stop(graceful=False)
		vm.reload()
		self.assertEqual(vm.status, "Stopped")
		self.assertEqual(mocked.call_args.kwargs["variables"]["GRACEFUL"], "0")

	def test_stop_forced_normalizes_stringy_flag(self) -> None:
		# REST/frm.call send a stringy value; "0"/"false" must read as forced, not
		# truthy-string. (Python bool("0") is True — the normalize guards that.)
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		for value in ("0", "false", "False"):
			vm = _new_vm()
			vm.db_set("status", "Running")
			vm.reload()
			with patch.object(module, "run_task", return_value=fake_task(name="task-stop")) as mocked:
				vm.stop(graceful=value)
			self.assertEqual(mocked.call_args.kwargs["variables"]["GRACEFUL"], "0", value)

	def test_stop_captures_memory_snapshot_when_opted_in(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module
		from atlas.atlas.networking import derive_uid

		vm = _new_vm(memory_snapshot_on_stop=1)
		vm.db_set("status", "Running")
		vm.reload()
		task = fake_task(
			name="task-stop-snap",
			stdout='ATLAS_RESULT={"memory_snapshot": true, "reason": "", "memory_snapshot_bytes": 536870912}',
		)
		with patch.object(module, "run_task", return_value=task) as mocked:
			vm.stop()
		vm.reload()
		self.assertEqual(vm.status, "Stopped")
		self.assertTrue(vm.has_memory_snapshot)
		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-stop-vm")
		# The jailed Firecracker writes the snapshot, so the script needs the
		# per-VM uid to hand it the directory.
		self.assertEqual(mocked.call_args.kwargs["variables"]["ATLAS_FC_UID"], str(derive_uid(vm.name)))

	def test_stop_fallback_leaves_flag_clear(self) -> None:
		# The script falls back to a plain stop on any snapshot failure and
		# reports memory_snapshot=false; the row must not claim a snapshot.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm(memory_snapshot_on_stop=1)
		vm.db_set("status", "Running")
		vm.reload()
		task = fake_task(
			name="task-stop-fallback",
			stdout='ATLAS_RESULT={"memory_snapshot": false, "reason": "API socket missing", "memory_snapshot_bytes": 0}',
		)
		with patch.object(module, "run_task", return_value=task):
			vm.stop()
		vm.reload()
		self.assertEqual(vm.status, "Stopped")
		self.assertFalse(vm.has_memory_snapshot)

	def test_stop_memory_snapshot_explicit_override(self) -> None:
		# stop(memory_snapshot=True) opts in for one stop without the field.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # field off
		vm.db_set("status", "Running")
		vm.reload()
		task = fake_task(
			name="task-stop-once",
			stdout='ATLAS_RESULT={"memory_snapshot": true, "reason": "", "memory_snapshot_bytes": 1}',
		)
		with patch.object(module, "run_task", return_value=task) as mocked:
			vm.stop(memory_snapshot=True)
		vm.reload()
		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-stop-vm")
		self.assertTrue(vm.has_memory_snapshot)

	def test_start_consumes_the_memory_snapshot(self) -> None:
		# The start consumes the on-host marker whether it restored or cold-booted,
		# so the flag clears unconditionally.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.db_set("has_memory_snapshot", 1)
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-start")):
			vm.start()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertFalse(vm.has_memory_snapshot)

	def test_restart_cold_skips_the_memory_snapshot(self) -> None:
		# cold=True is the true-reboot escape hatch even on an opted-in VM:
		# plain stop, full cold boot.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm(memory_snapshot_on_stop=1)
		vm.db_set("status", "Running")
		vm.reload()
		stop_task = fake_task(name="task-stop")
		start_task = fake_task(name="task-start")
		with patch.object(module, "run_task", side_effect=[stop_task, start_task]) as mocked:
			vm.restart(cold=True)
		self.assertEqual(mocked.call_args_list[0].kwargs["script"], "stop-vm")
		self.assertEqual(mocked.call_args_list[1].kwargs["script"], "start-vm")

	def test_restart_power_cycles_via_memory_snapshot(self) -> None:
		# An opted-in VM's restart is a state-preserving power cycle.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm(memory_snapshot_on_stop=1)
		vm.db_set("status", "Running")
		vm.reload()
		stop_task = fake_task(
			name="task-stop-snap",
			stdout='ATLAS_RESULT={"memory_snapshot": true, "reason": "", "memory_snapshot_bytes": 1}',
		)
		start_task = fake_task(name="task-start")
		with patch.object(module, "run_task", side_effect=[stop_task, start_task]) as mocked:
			vm.restart()
		self.assertEqual(mocked.call_args_list[0].kwargs["script"], "snapshot-stop-vm")
		self.assertEqual(mocked.call_args_list[1].kwargs["script"], "start-vm")

	def test_resize_invalidates_the_memory_snapshot(self) -> None:
		# resize-vm.py drops the on-host snapshot (vmstate no longer matches the
		# machine config); the row must mirror it.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.db_set("has_memory_snapshot", 1)
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=1024)
		vm.reload()
		self.assertFalse(vm.has_memory_snapshot)

	def test_rebuild_invalidates_the_memory_snapshot(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Stopped")
		vm.db_set("has_memory_snapshot", 1)
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-rebuild")):
			vm.rebuild(source_type="image")
		vm.reload()
		self.assertFalse(vm.has_memory_snapshot)

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

	# --- resize capacity gate (spec/28) ------------------------------------

	def test_resize_within_capacity_passes(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		server = _resize_server(memory_megabytes_total=4096)
		vm = make_virtual_machine(server, _ensure_test_image(), memory_megabytes=512, disk_gigabytes=4)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-resize")) as mocked:
			vm.resize(memory_megabytes=1024)
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 1024)
		mocked.assert_called_once()

	def test_resize_over_capacity_raises(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		# Host holds exactly 1024 MB and the VM already fills it; doubling the RAM
		# doesn't fit → NoResizeCapacityError, and the on-host resize never runs.
		server = _resize_server(memory_megabytes_total=1024)
		vm = make_virtual_machine(server, _ensure_test_image(), memory_megabytes=1024, disk_gigabytes=4)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(NoResizeCapacityError):
				vm.resize(memory_megabytes=2048)
		mocked.assert_not_called()

	def test_resize_charges_only_positive_deltas(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		# CPU is measured and already full (1 core on a 1-core host), but growing RAM
		# only charges the RAM delta — the unchanged CPU axis must not block it.
		server = _resize_server(vcpus_total=1, memory_megabytes_total=4096)
		vm = make_virtual_machine(
			server, _ensure_test_image(), vcpus=1, cpu_max_cores=1, memory_megabytes=512, disk_gigabytes=4
		)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=1024)
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 1024, "a full CPU axis doesn't block a RAM-only grow")

	def test_resize_spends_the_placement_reserve(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		# A 50% arrival reserve would refuse PLACING a new VM past 512 MB, but resize
		# checks the FULL effective budget (1024) — so it spends into the reserve.
		server = _resize_server(memory_megabytes_total=1024)
		frappe.db.set_single_value("Atlas Settings", "placement_headroom_percent", 50)
		self.addCleanup(frappe.db.set_single_value, "Atlas Settings", "placement_headroom_percent", 0)
		vm = make_virtual_machine(server, _ensure_test_image(), memory_megabytes=512, disk_gigabytes=4)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=1024)
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 1024, "resize spends the headroom placement reserved")

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
