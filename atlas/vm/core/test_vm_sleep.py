from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from urllib.parse import urlsplit
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.atlas.object_storage import ObjectStorageClient
from atlas.vm.core import vm_sleep
from atlas.vm.core.metal_client import MetalClient
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_sleep import VirtualMachineSleepService


def object_storage() -> ObjectStorageClient:
	return ObjectStorageClient(
		bucket="snapshots",
		access_key_id="test-access",
		secret_access_key="test-secret",
		endpoint_url="https://objects.example",
		region="test-region",
	)


def sleeping_response(state: str, snapshot: dict | None) -> MetalVirtualMachine:
	response = complete_response()
	response["observed"].update(state="stopped", sleep_state=state, sleep_snapshot=snapshot)
	return MetalVirtualMachine.from_dict(response)


def object_path(url: str) -> str:
	return urlsplit(url).path.removeprefix("/snapshots/")


class TestVirtualMachineSleepPolicy(UnitTestCase):
	def service(self, **values) -> VirtualMachineSleepService:
		machine = SimpleNamespace(name="vm-1", server="host-1", memory_mib=2048, disk_mib=10240, **values)
		service = VirtualMachineSleepService(machine)
		service.storage = object_storage()
		return service

	def test_refresh_after_migration_keeps_each_artifact_at_its_original_key(self) -> None:
		service = self.service()
		client = Mock(spec=MetalClient)
		information = sleeping_response(
			"coma",
			{
				"id": "checkpoint-moved",
				"memory": {"uploaded": True, "object_key": "sleep/test.local/host-a/vm-1/memory"},
				"disk": {
					"uploaded": True,
					"object_key": "sleep/test.local/vm-1/abc/disk",
					"artifact": {
						"size_bytes": 1,
						"stored_size_bytes": 1,
						"sha256": "a" * 64,
						"part_count": 3,
					},
				},
			},
		)

		service.refresh_urls(information, client)

		request = client.refresh_sleep_state.call_args.args[1]
		self.assertEqual(request.pop("snapshot_id"), "checkpoint-moved")
		self.assertEqual(
			object_path(request["memory"]["download_url"]), "sleep/test.local/host-a/vm-1/memory"
		)
		self.assertEqual(
			[object_path(part["download_url"]) for part in request["disk"]["parts"]],
			[f"sleep/test.local/vm-1/abc/disk.{number}" for number in (1, 2, 3)],
		)

	def test_an_artifact_that_is_still_uploading_keeps_its_upload_urls(self) -> None:
		client = Mock(spec=MetalClient)
		information = sleeping_response("sleeping", {"id": "checkpoint-1", "memory": {"uploaded": False}})

		self.service().refresh_urls(information, client)

		client.refresh_sleep_state.assert_not_called()

	def test_offload_signs_enough_parts_in_one_checkpoint_directory(self) -> None:
		client = Mock(spec=MetalClient)
		with patch.object(vm_sleep.frappe, "generate_hash", return_value="new"):
			self.service().offload(sleeping_response("sleeping", None), client, ["memory", "disk"])
		request = client.offload_sleep_state.call_args.args[1]

		self.assertEqual(request["memory"]["object_key"], f"sleep/{frappe.local.site}/vm-1/new/memory")
		self.assertEqual(len(request["memory"]["parts"]), 2048 // 512 + 2)
		self.assertEqual(len(request["disk"]["parts"]), 10240 // 512 + 2)
		self.assertEqual(request["disk"]["part_size_bytes"], 512 << 20)
		self.assertEqual(
			object_path(request["disk"]["parts"][1]["upload_url"]),
			f"sleep/{frappe.local.site}/vm-1/new/disk.2",
		)

		# A later disk upload joins the directory of its checkpoint.
		existing = sleeping_response(
			"deep-sleeping",
			{
				"id": "checkpoint-1",
				"memory": {"uploaded": True, "object_key": "sleep/test.local/vm-1/old/memory"},
			},
		)
		self.service().offload(existing, client, ["disk"])
		self.assertEqual(
			client.offload_sleep_state.call_args.args[1]["disk"]["object_key"],
			"sleep/test.local/vm-1/old/disk",
		)

	def test_a_wake_or_a_new_checkpoint_deletes_only_unused_objects(self) -> None:
		current = sleeping_response(
			"deep-sleeping",
			{
				"id": "checkpoint-2",
				"memory": {"uploaded": True, "object_key": "sleep/test.local/vm-1/new/memory"},
			},
		).observed.sleep_snapshot
		site = frappe.local.site
		for signed, snapshot, expected in (
			(None, current, []),
			("checkpoint-2", current, []),
			("checkpoint-1", current, [call(f"sleep/{site}/vm-1/", keep={"sleep/test.local/vm-1/new"})]),
			(
				"checkpoint-1",
				None,
				[call(f"sleep/{site}/vm-1/", keep=set()), call(f"sleep/{site}/host-1/vm-1/")],
			),
		):
			service = self.service()
			service.storage = Mock(spec=ObjectStorageClient)
			with (
				self.subTest(signed=signed, snapshot=snapshot),
				patch.object(vm_sleep.frappe.db, "get_value", return_value=signed),
			):
				service.delete_unused_objects(snapshot)
			self.assertEqual(service.storage.delete_prefix.call_args_list, expected)

	def test_only_an_awake_vm_loses_its_objects(self) -> None:
		for state, sleep_state, deleted in (
			("running", "", True),
			("failed", "coma", False),
			("stopped", "coma", False),
		):
			service = self.service(
				is_draft=0,
				is_terminating=0,
				active_migration=None,
				deep_sleep_after_idle_seconds=0,
				coma_after_idle_seconds=0,
			)
			service.storage = Mock(spec=ObjectStorageClient)
			client = Mock(spec=MetalClient)
			response = complete_response()
			response["observed"].update(state=state, sleep_state=sleep_state, sleep_snapshot=None)
			client.get_virtual_machine.return_value = MetalVirtualMachine.from_dict(response)
			with (
				self.subTest(state=state, sleep_state=sleep_state),
				patch.object(vm_sleep.VirtualMachineService, "metal_client", client),
				patch.object(vm_sleep.frappe.db, "get_value", return_value="checkpoint-1"),
				patch.object(vm_sleep.frappe.db, "set_value"),
				patch("frappe.utils.data.get_system_timezone", return_value="UTC"),
			):
				service.reconcile()
			self.assertEqual(service.storage.delete_prefix.called, deleted)

	def test_stored_host_state_selects_due_work(self) -> None:
		now = datetime(2026, 10, 23, 12)
		renew_before = now + timedelta(hours=12)
		base = frappe._dict(
			memory_mib=2048,
			disk_mib=10240,
			deep_sleep=3600,
			coma=7200,
			sleep_state="deep-sleeping",
			sleep_snapshot_id="checkpoint-1",
			sleep_urls_snapshot_id="checkpoint-1",
			sleep_urls_expire_at=now + timedelta(hours=20),
			last_request_at=now - timedelta(minutes=90),
		)
		for changes, expected in (
			({}, False),
			({"sleep_urls_expire_at": now + timedelta(hours=11)}, True),
			({"sleep_urls_expire_at": None}, True),
			({"sleep_snapshot_id": "checkpoint-2"}, True),
			({"sleep_state": "", "sleep_snapshot_id": None}, True),
			({"sleep_state": "", "sleep_snapshot_id": None, "sleep_urls_snapshot_id": None}, False),
			({"last_request_at": now - timedelta(hours=3)}, True),
			({"last_request_at": now - timedelta(hours=3), "disk_mib": 102400}, False),
			({"sleep_state": "coma", "last_request_at": now - timedelta(hours=3)}, False),
			({"sleep_state": "sleeping", "sleep_snapshot_id": None, "sleep_urls_snapshot_id": None}, True),
			(
				{
					"sleep_state": "sleeping",
					"sleep_snapshot_id": None,
					"sleep_urls_snapshot_id": None,
					"last_request_at": now - timedelta(minutes=30),
				},
				False,
			),
		):
			with self.subTest(changes=changes):
				row = frappe._dict(base, **changes)
				self.assertEqual(VirtualMachineSleepService.is_due(row, now, renew_before), expected)

	def test_idle_thresholds_select_memory_before_disk(self) -> None:
		information = MetalVirtualMachine.from_dict(complete_response())
		information = replace(
			information,
			observed=replace(information.observed, state="stopped", sleep_state="sleeping"),
		)
		last_request = datetime(2026, 9, 6, 9, 59, 30)
		for state, idle, expected in (
			("sleeping", 3599, []),
			("sleeping", 3600, ["memory"]),
			("sleeping", 7200, ["memory", "disk"]),
			("deep-sleeping", 3600, []),
			("deep-sleeping", 7200, ["disk"]),
			("coma", 7200, []),
		):
			with (
				self.subTest(state=state, idle=idle),
				patch("frappe.utils.data.get_system_timezone", return_value="UTC"),
			):
				machine = replace(information, observed=replace(information.observed, sleep_state=state))
				self.assertEqual(
					VirtualMachineSleepService.artifacts_to_offload(
						machine, 3600, 7200, last_request + timedelta(seconds=idle)
					),
					expected,
				)

	def test_disabled_thresholds_and_missing_request_times_do_not_offload(self) -> None:
		response = complete_response()
		response["observed"].update(state="stopped", sleep_state="sleeping")
		information = MetalVirtualMachine.from_dict(response)
		for deep, coma, requested, expected in (
			(0, 0, "2026-09-06T09:59:30Z", []),
			(3600, 0, "2026-09-06T09:59:30Z", ["memory"]),
			(3600, 7200, "", []),
			(3600, 7200, "0001-01-01T00:00:00Z", []),
			(3600, 7200, "2026-09-07T10:00:01Z", []),
		):
			with (
				self.subTest(deep=deep, coma=coma, requested=requested),
				patch("frappe.utils.data.get_system_timezone", return_value="UTC"),
			):
				machine = replace(
					information, observed=replace(information.observed, last_request_at=requested)
				)
				self.assertEqual(
					VirtualMachineSleepService.artifacts_to_offload(
						machine, deep, coma, datetime(2026, 9, 7, 10)
					),
					expected,
				)

	def test_manual_stop_and_disabled_sleep_do_not_offload(self) -> None:
		information = MetalVirtualMachine.from_dict(complete_response())
		information = replace(
			information,
			observed=replace(information.observed, state="stopped", sleep_state="sleeping"),
		)
		for desired in (
			replace(information.desired, state="stopped"),
			replace(
				information.desired, compute=replace(information.desired.compute, sleep_after_idle_seconds=0)
			),
		):
			self.assertEqual(
				VirtualMachineSleepService.artifacts_to_offload(
					replace(information, desired=desired), 3600, 7200, datetime(2026, 9, 7, 10)
				),
				[],
			)

	def test_threshold_comparison_uses_the_site_timezone(self) -> None:
		information = MetalVirtualMachine.from_dict(complete_response())
		information = replace(
			information,
			observed=replace(information.observed, state="stopped", sleep_state="sleeping"),
		)
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(
				VirtualMachineSleepService.artifacts_to_offload(
					information, 3600, 7200, datetime(2026, 9, 6, 16, 29, 30)
				),
				["memory"],
			)


class TestDueSleepWork(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = str(uuid7())

	def insert(self, sleep_state: str, **overrides) -> str:
		"""Write one sleepy VM row and its reported host state without a host."""
		name = frappe.generate_hash(length=10)
		virtual_machine = frappe.new_doc("Virtual Machine")
		virtual_machine.update(
			{
				"name": name,
				"server": self.server,
				"virtual_machine_image": "image-1",
				"architecture": "amd64",
				"cpu_millicores": 1000,
				"memory_mib": 1024,
				"disk_mib": 10240,
				"tenant_id": 7,
				"sleep_after_idle_seconds": 60,
				"deep_sleep_after_idle_seconds": 3600,
				**overrides,
			}
		)
		virtual_machine.db_insert()
		state = frappe.new_doc("Virtual Machine State")
		state.update(
			{
				"name": name,
				"virtual_machine": name,
				"server": self.server,
				"status": "stopped",
				"sleep_state": sleep_state,
				"last_request_at": datetime(2026, 10, 23, 9),
			}
		)
		state.db_insert()
		return name

	def test_stored_state_selects_vms_without_a_host_call(self) -> None:
		due = self.insert("sleeping")
		migrating = self.insert("sleeping", active_migration="migration-1")
		awake = self.insert("")

		names = vm_sleep.get_due_virtual_machines(datetime(2026, 10, 23, 12))

		self.assertIn(due, names)
		self.assertNotIn(migrating, names)
		self.assertNotIn(awake, names)
