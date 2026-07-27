"""Unit tests for the Sleepy VMs feature (spec/32).

Exercises:
- sleep_on_idle validate constraint (idle_timeout_seconds >= 120)
- start() from Sleeping delegates to wake()
- stop() / snapshot() from Sleeping throw
- Capacity accounting: Sleeping excluded from RAM/CPU axes, included in disk
- poll_vm_traffic() and sleep_idle_vms() scheduler functions, over the
  non-persisting run_probe path (no Task rows for the per-minute sweeps)
- reconcile_sleeping_vms() / _adopt_wake(): adopting a host-initiated
  (packet-triggered) wake into the DB, race-safe against an operator wake()
- The idle clock is reset on every transition into Running (provision/start/wake)
- fake_tasks: sleep-vm, poll-vm-traffic and probe-woken-vms result builders

The host-side wake trap itself (park/unpark, the atlas-wake-trap daemon) is
covered by scripts/lib/atlas/test_{park,wake_trap}.py — bare unittest, since the
Frappe app shares the `atlas` name with the scripts package they import.
"""

import json

import frappe
import frappe.model.document
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import server_capacity
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _clean_virtual_machines() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestSleepyVmsCapacity(IntegrationTestCase):
	"""Sleeping VMs are excluded from RAM/CPU axes but still charged for disk."""

	def setUp(self) -> None:
		_clean_virtual_machines()
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 0)
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		self.provider = make_provider("sleepy-cap-provider")
		self.server = make_server(
			self.provider,
			"sleepy-cap-server",
			ipv4_address="10.0.99.1",
			ipv6_address="2001:db8:99::1",
			ipv6_prefix="2001:db8:99::/64",
			ipv6_virtual_machine_range="2001:db8:99::/124",
			status="Active",
		)
		self.server.db_set("memory_megabytes_total", 4096)
		self.server.db_set("pool_disk_gigabytes_total", 100)
		self.image = make_image("sleepy-cap-image")

	def test_sleeping_vm_excluded_from_memory_used(self) -> None:
		vm = make_virtual_machine(self.server, self.image, memory_megabytes=512, disk_gigabytes=10)
		vm.db_set("status", "Sleeping")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["memory"]["used"], 0, "sleeping VM's RAM should not count")

	def test_sleeping_vm_excluded_from_cpu_used(self) -> None:
		vm = make_virtual_machine(self.server, self.image, vcpus=2, memory_megabytes=512, disk_gigabytes=5)
		vm.db_set("status", "Sleeping")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["used"], 0, "sleeping VM's CPU should not count")

	def test_sleeping_vm_still_charged_for_disk(self) -> None:
		vm = make_virtual_machine(
			self.server, self.image, memory_megabytes=512, disk_gigabytes=10, data_disk_gigabytes=5
		)
		vm.db_set("status", "Sleeping")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["disk"]["used"], 15, "sleeping VM's disk is still allocated")

	def test_running_and_sleeping_vms_mixed(self) -> None:
		make_virtual_machine(self.server, self.image, memory_megabytes=512, disk_gigabytes=10)
		sleeping = make_virtual_machine(
			self.server, self.image, memory_megabytes=1024, disk_gigabytes=20
		)
		sleeping.db_set("status", "Sleeping")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["memory"]["used"], 512, "only running VM's RAM counted")
		self.assertEqual(result["disk"]["used"], 30, "both VMs' disk counted")
		self.assertEqual(result["virtual_machine_count"], 1, "only running VM in resident count")

	def test_terminated_and_sleeping_vm_disk_exclusion(self) -> None:
		terminated = make_virtual_machine(
			self.server, self.image, memory_megabytes=512, disk_gigabytes=50
		)
		terminated.db_set("status", "Terminated")
		sleeping = make_virtual_machine(
			self.server, self.image, memory_megabytes=512, disk_gigabytes=10
		)
		sleeping.db_set("status", "Sleeping")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["disk"]["used"], 10, "only sleeping VM disk counted; terminated excluded")
		self.assertEqual(result["memory"]["used"], 0, "neither terminated nor sleeping count for RAM")


class TestSleepyVmsLifecycle(IntegrationTestCase):
	"""VM controller guards for the Sleeping status."""

	def setUp(self) -> None:
		_clean_virtual_machines()
		self.provider = make_provider("sleepy-lc-provider")
		self.server = make_server(
			self.provider,
			"sleepy-lc-server",
			ipv4_address="10.0.98.1",
			ipv6_address="2001:db8:98::1",
			ipv6_prefix="2001:db8:98::/64",
			ipv6_virtual_machine_range="2001:db8:98::/124",
			status="Active",
		)
		self.image = make_image("sleepy-lc-image")

	def _make_sleeping_vm(self, **overrides) -> frappe.model.document.Document:
		vm = make_virtual_machine(self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=120, **overrides)
		vm.db_set("status", "Sleeping")
		vm.reload()
		return vm

	def test_validate_rejects_timeout_below_120(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			make_virtual_machine(
				self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=60
			)

	def test_validate_rejects_zero_timeout_with_sleep_on_idle(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			make_virtual_machine(
				self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=0
			)

	def test_validate_allows_120_seconds(self) -> None:
		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=120
		)
		self.assertIsNotNone(vm.name)

	def test_validate_allows_no_timeout_when_sleep_on_idle_off(self) -> None:
		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=0, idle_timeout_seconds=0
		)
		self.assertIsNotNone(vm.name)

	def test_stop_from_sleeping_throws(self) -> None:
		vm = self._make_sleeping_vm()
		with self.assertRaises(frappe.ValidationError):
			vm.stop()

	def test_snapshot_from_sleeping_throws(self) -> None:
		vm = self._make_sleeping_vm()
		with self.assertRaises(frappe.ValidationError):
			vm.snapshot()

	def test_snapshot_live_from_sleeping_throws(self) -> None:
		vm = self._make_sleeping_vm()
		with self.assertRaises(frappe.ValidationError):
			vm.snapshot(live=True)

	def test_start_from_sleeping_calls_wake(self) -> None:
		"""start() from Sleeping must delegate to wake(), not throw."""
		vm = self._make_sleeping_vm()
		wake_calls = []

		def _capture_wake():
			wake_calls.append(True)
			return "fake-task"

		vm.wake = _capture_wake
		result = vm.start()
		self.assertEqual(result, "fake-task")
		self.assertEqual(len(wake_calls), 1, "start() must delegate to wake() from Sleeping")

	def test_sleep_requires_sleep_on_idle(self) -> None:
		vm = make_virtual_machine(self.server, self.image, sleep_on_idle=0, idle_timeout_seconds=0)
		vm.db_set("status", "Running")
		vm.reload()
		with self.assertRaises(frappe.ValidationError):
			vm.sleep()

	def test_sleep_from_non_running_throws(self) -> None:
		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=300
		)
		vm.db_set("status", "Stopped")
		vm.reload()
		with self.assertRaises(frappe.ValidationError):
			vm.sleep()

	def test_migration_rejects_a_sleeping_vm_by_name(self) -> None:
		"""spec/32: the memory snapshot is not transportable, so migration from
		Sleeping is refused — and says which action unblocks it."""
		from atlas.atlas.migration import preflight_checks

		vm = self._make_sleeping_vm()
		with self.assertRaises(frappe.ValidationError) as caught:
			preflight_checks(vm, self.server.name, release_reserved_ip=False)
		self.assertIn("wake or stop", str(caught.exception).lower())

	def test_wake_from_non_sleeping_throws(self) -> None:
		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=300
		)
		vm.db_set("status", "Running")
		vm.reload()
		with self.assertRaises(frappe.ValidationError):
			vm.wake()


class TestSleepyVmsFakeTasks(IntegrationTestCase):
	"""sleep-vm and poll-vm-traffic use the Fake task seam correctly."""

	def setUp(self) -> None:
		_clean_virtual_machines()
		from atlas.atlas.providers.fake_tasks import _poll_vm_traffic_result, _sleep_vm_result

		self._sleep_vm_result = _sleep_vm_result
		self._poll_vm_traffic_result = _poll_vm_traffic_result

	def test_sleep_vm_result_has_memory_snapshot_true(self) -> None:
		result = self._sleep_vm_result({})
		self.assertTrue(result["memory_snapshot"])
		self.assertIn("memory_snapshot_bytes", result)
		self.assertIn("reason", result)

	def test_poll_vm_traffic_result_has_active_false_per_vm(self) -> None:
		vms = [{"name": "vm-1"}, {"name": "vm-2"}]
		result = self._poll_vm_traffic_result({"VMS_JSON": json.dumps(vms)})
		self.assertIn("counters", result)
		self.assertFalse(result["counters"]["vm-1"]["active"])
		self.assertFalse(result["counters"]["vm-2"]["active"])

	def test_poll_vm_traffic_result_empty_vms_json(self) -> None:
		result = self._poll_vm_traffic_result({})
		self.assertEqual(result["counters"], {})

	def test_poll_vm_traffic_result_bad_json_returns_empty(self) -> None:
		result = self._poll_vm_traffic_result({"VMS_JSON": "not-json"})
		self.assertEqual(result["counters"], {})

	def test_probe_woken_vms_result_reports_none_woken(self) -> None:
		from atlas.atlas.providers.fake_tasks import _probe_woken_vms_result

		result = _probe_woken_vms_result({"VMS_JSON": json.dumps(["u1", "u2"])})
		self.assertEqual(result["woken"], {"u1": False, "u2": False})

	def test_probe_woken_vms_result_bad_json_returns_empty(self) -> None:
		from atlas.atlas.providers.fake_tasks import _probe_woken_vms_result

		self.assertEqual(_probe_woken_vms_result({"VMS_JSON": "not-json"}), {"woken": {}})


class TestReconcileSleepingVms(IntegrationTestCase):
	"""reconcile_sleeping_vms() adopts a host-initiated (packet-triggered) wake into
	the Frappe status, race-safe against an operator wake()."""

	def setUp(self) -> None:
		_clean_virtual_machines()
		self.provider = make_provider("sleepy-rec-provider")
		self.server = make_server(
			self.provider,
			"sleepy-rec-server",
			ipv4_address="10.0.97.1",
			ipv6_address="2001:db8:97::1",
			ipv6_prefix="2001:db8:97::/64",
			ipv6_virtual_machine_range="2001:db8:97::/124",
			status="Active",
		)
		self.image = make_image("sleepy-rec-image")

	def _sleeping_vm(self, **overrides) -> frappe.model.document.Document:
		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=120, **overrides
		)
		vm.db_set("status", "Sleeping")
		vm.reload()
		return vm

	def _probe_stdout(self, woken: dict) -> str:
		return "ATLAS_RESULT=" + json.dumps({"woken": woken}) + "\n"

	def test_adopt_wake_flips_sleeping_to_running(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._sleeping_vm()
		vm.db_set("has_memory_snapshot", 1)
		vm_mod._adopt_wake(vm.name, frappe.utils.now_datetime())
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertEqual(vm.has_memory_snapshot, 0, "the wake consumed the snapshot")
		self.assertIsNotNone(vm.last_started)
		self.assertIsNotNone(vm.last_traffic_at, "fresh so sleep_idle_vms won't re-sleep it")

	def test_adopt_wake_noop_when_operator_wake_won_the_race(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._sleeping_vm()
		vm.db_set("status", "Running")  # operator wake() already flipped it
		vm_mod._adopt_wake(vm.name, frappe.utils.now_datetime())  # must not throw
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_reconcile_adopts_woken_vm(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._sleeping_vm()
		stdout = self._probe_stdout({vm.name: True})
		with patch.object(vm_mod, "run_probe", return_value=stdout) as mocked:
			vm_mod.reconcile_sleeping_vms()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		# Server-scoped probe on the non-persisting path: the probe-woken-vms verb,
		# via run_probe so the per-minute sweep records no Task row.
		_, kwargs = mocked.call_args
		self.assertEqual(kwargs.get("script"), "probe-woken-vms")
		self.assertNotIn("virtual_machine", kwargs, "run_probe is server-scoped")

	def test_reconcile_leaves_unwoken_vm_sleeping(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._sleeping_vm()
		stdout = self._probe_stdout({vm.name: False})
		with patch.object(vm_mod, "run_probe", return_value=stdout):
			vm_mod.reconcile_sleeping_vms()
		vm.reload()
		self.assertEqual(vm.status, "Sleeping")

	def test_reconcile_tolerates_a_failed_probe(self) -> None:
		"""run_probe returns "" on failure rather than raising; the VM stays put."""
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._sleeping_vm()
		with patch.object(vm_mod, "run_probe", return_value=""):
			vm_mod.reconcile_sleeping_vms()
		vm.reload()
		self.assertEqual(vm.status, "Sleeping")

	def test_poll_vm_traffic_stamps_active_vms(self) -> None:
		"""An active VM gets a fresh last_traffic_at; an idle one is left alone."""
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		active = self._sleeping_vm()
		idle = self._sleeping_vm()
		for vm in (active, idle):
			vm.db_set("status", "Running")
			vm.db_set("last_traffic_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1))
		stale = frappe.db.get_value("Virtual Machine", idle.name, "last_traffic_at")

		stdout = "ATLAS_RESULT=" + json.dumps(
			{"counters": {active.name: {"active": True}, idle.name: {"active": False}}}
		)
		with patch.object(vm_mod, "run_probe", return_value=stdout) as mocked:
			vm_mod.poll_vm_traffic()

		self.assertGreater(
			frappe.db.get_value("Virtual Machine", active.name, "last_traffic_at"), stale
		)
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", idle.name, "last_traffic_at"), stale
		)
		_, kwargs = mocked.call_args
		self.assertEqual(kwargs.get("script"), "poll-vm-traffic")
		self.assertNotIn("virtual_machine", kwargs, "run_probe is server-scoped")

	def test_poll_vm_traffic_tolerates_a_failed_probe(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._sleeping_vm()
		vm.db_set("status", "Running")
		with patch.object(vm_mod, "run_probe", return_value=""):
			vm_mod.poll_vm_traffic()  # must not raise

	def test_reconcile_no_sleeping_vms_skips_ssh(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		with patch.object(vm_mod, "run_probe") as mocked:
			vm_mod.reconcile_sleeping_vms()
		mocked.assert_not_called()


class TestIdleClockSeeding(IntegrationTestCase):
	"""last_traffic_at is stamped at provision/start/wake (spec/32).

	Every transition into Running must reset the idle clock. A VM that slept did
	so *because* last_traffic_at was older than idle_timeout_seconds, so waking it
	without re-stamping leaves the field stale and the very next sleep_idle_vms
	tick — within a minute — puts it straight back to sleep.
	"""

	def setUp(self) -> None:
		_clean_virtual_machines()
		self.provider = make_provider("sleepy-clock-provider")
		self.server = make_server(
			self.provider,
			"sleepy-clock-server",
			ipv4_address="10.0.96.1",
			ipv6_address="2001:db8:96::1",
			ipv6_prefix="2001:db8:96::/64",
			ipv6_virtual_machine_range="2001:db8:96::/124",
			status="Active",
		)
		self.image = make_image("sleepy-clock-image")

	def _stale_vm(self, status: str) -> frappe.model.document.Document:
		"""A sleep_on_idle VM whose idle clock is already well past its timeout."""
		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=120
		)
		vm.db_set("status", status)
		vm.db_set("last_traffic_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1))
		vm.reload()
		return vm

	def _fake_task(self):
		"""A Task stub whose stdout parses as a real sleep-vm result.

		It must: sleep() does `parse_result(task.stdout)["memory_snapshot"]`, and
		sleep_idle_vms swallows exceptions — so an unparseable stdout would make a
		re-sleep look like a pass in test_woken_vm_is_not_immediately_re_slept.
		"""
		from types import SimpleNamespace

		return SimpleNamespace(name="t", stdout='ATLAS_RESULT={"memory_snapshot": true}\n')

	def test_wake_refreshes_the_idle_clock(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._stale_vm("Sleeping")
		before = vm.last_traffic_at
		with patch.object(vm_mod, "run_task", return_value=self._fake_task()):
			vm.wake()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertGreater(vm.last_traffic_at, before, "wake() must reset the idle clock")

	def test_start_refreshes_the_idle_clock(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._stale_vm("Stopped")
		before = vm.last_traffic_at
		with patch.object(vm_mod, "run_task", return_value=self._fake_task()):
			vm.start()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertGreater(vm.last_traffic_at, before, "start() must reset the idle clock")

	def test_provision_seeds_the_idle_clock(self) -> None:
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = make_virtual_machine(
			self.server, self.image, sleep_on_idle=1, idle_timeout_seconds=120
		)
		vm.db_set("status", "Pending")
		vm.db_set("last_traffic_at", None)
		vm.reload()
		with patch.object(vm_mod, "run_task", return_value=self._fake_task()):
			vm.provision()
		vm.reload()
		self.assertIsNotNone(vm.last_traffic_at, "a fresh VM must start with a seeded idle clock")

	def test_woken_vm_is_not_immediately_re_slept(self) -> None:
		"""The regression this guards: wake() then the very next idle sweep."""
		from unittest.mock import patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_mod

		vm = self._stale_vm("Sleeping")
		with patch.object(vm_mod, "run_task", return_value=self._fake_task()):
			vm.wake()
			vm_mod.sleep_idle_vms()
		vm.reload()
		self.assertEqual(vm.status, "Running", "a just-woken VM must survive the next idle sweep")
