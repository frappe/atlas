from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import urlsplit

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.object_storage import ObjectStorageClient
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_service import VirtualMachineService
from atlas.vm.core.vm_sleep import VirtualMachineSleepService


class TestVirtualMachineSleepPolicy(UnitTestCase):
	def test_each_vm_controls_offload_without_disabling_recovery_url_refresh(self) -> None:
		storage = ObjectStorageClient(
			bucket="snapshots",
			access_key_id="test-access",
			secret_access_key="test-secret",
			endpoint_url="https://objects.example",
			region="test-region",
		)
		settings = SimpleNamespace(
			get_object_storage_client=lambda: storage,
		)
		virtual_machine = SimpleNamespace(
			name="vm-1",
			server="host-1",
			is_draft=False,
			is_terminating=False,
			active_migration=None,
			deep_sleep_after_idle_seconds=0,
			coma_after_idle_seconds=0,
		)
		for state, artifacts, deep_sleep in (
			("deep-sleeping", {"memory": {"uploaded": True}}, 0),
			("comatose", {"memory": {"uploaded": True}, "disk": {"uploaded": True}}, 0),
			("sleeping", None, 3600),
			("sleeping", None, 0),
		):
			virtual_machine.deep_sleep_after_idle_seconds = deep_sleep
			response = complete_response()
			response["observed"].update(
				state="stopped",
				sleep_state=state,
				sleep_snapshot={"id": "checkpoint-1", **artifacts} if artifacts else None,
			)
			client = Mock(spec=MetalClient)
			client.get_virtual_machine.return_value = MetalVirtualMachine.from_dict(response)
			refresh_key = f"atlas:vm-sleep-urls:{virtual_machine.name}:checkpoint-1"
			frappe.cache.delete_value(refresh_key)
			self.addCleanup(frappe.cache.delete_value, refresh_key)
			with (
				self.subTest(state=state, deep_sleep=deep_sleep),
				patch("atlas.vm.core.vm_sleep.now_datetime", return_value=datetime(2026, 9, 7, 10)),
				patch("frappe.utils.data.get_system_timezone", return_value="UTC"),
				patch.object(
					VirtualMachineService, "metal_client", new_callable=PropertyMock, return_value=client
				),
				patch("atlas.vm.core.vm_sleep.frappe.get_cached_doc", return_value=settings),
			):
				VirtualMachineSleepService(virtual_machine).reconcile()

			client.set_virtual_machine_power_state.assert_not_called()
			if deep_sleep:
				self.assertEqual(set(client.offload_sleep_state.call_args.args[1]), {"memory"})
				client.refresh_sleep_state.assert_not_called()
				continue
			client.offload_sleep_state.assert_not_called()
			if artifacts is None:
				client.refresh_sleep_state.assert_not_called()
				continue
			request = client.refresh_sleep_state.call_args.args[1]
			self.assertEqual(request.pop("snapshot_id"), "checkpoint-1")
			self.assertEqual(set(request), set(artifacts))
			for name, target in request.items():
				self.assertEqual(set(target), {"download_url", "object_key"})
				self.assertTrue(urlsplit(target["download_url"]).path.endswith(f"/host-1/vm-1/{name}"))

	def test_hourly_refresh_does_not_delay_offload_or_failed_refresh_retries(self) -> None:
		storage = ObjectStorageClient(
			bucket="snapshots",
			access_key_id="test-access",
			secret_access_key="test-secret",
			endpoint_url="https://objects.example",
			region="test-region",
		)
		machine = SimpleNamespace(
			name="vm-" + frappe.generate_hash(length=10),
			server="host-1",
			is_draft=False,
			is_terminating=False,
			active_migration=None,
			deep_sleep_after_idle_seconds=0,
			coma_after_idle_seconds=0,
		)
		response = complete_response()
		response["observed"].update(
			state="stopped",
			sleep_state="deep-sleeping",
			sleep_snapshot={"id": "checkpoint-1", "memory": {"uploaded": True}},
		)
		client = Mock(spec=MetalClient)
		client.get_virtual_machine.return_value = MetalVirtualMachine.from_dict(response)
		cache = frappe.cache()
		refresh_key = f"atlas:vm-sleep-urls:{machine.name}:checkpoint-1"
		self.addCleanup(cache.delete_value, refresh_key)
		with (
			patch.object(
				VirtualMachineService, "metal_client", new_callable=PropertyMock, return_value=client
			),
			patch(
				"atlas.vm.core.vm_sleep.frappe.get_cached_doc",
				return_value=SimpleNamespace(get_object_storage_client=lambda: storage),
			),
			patch("atlas.vm.core.vm_sleep.now_datetime", return_value=datetime(2026, 9, 7, 10)),
			patch("frappe.utils.data.get_system_timezone", return_value="UTC"),
		):
			VirtualMachineSleepService(machine).reconcile()
			VirtualMachineSleepService(machine).reconcile()
			client.refresh_sleep_state.assert_called_once()
			self.assertTrue(3500 < cache.ttl(cache.make_key(refresh_key)) <= 3600)

			machine.deep_sleep_after_idle_seconds = 3600
			machine.coma_after_idle_seconds = 7200
			VirtualMachineSleepService(machine).reconcile()
			client.refresh_sleep_state.assert_called_once()
			self.assertEqual(set(client.offload_sleep_state.call_args.args[1]), {"disk"})
			machine.deep_sleep_after_idle_seconds = machine.coma_after_idle_seconds = 0

			cache.expire_key(refresh_key, 0)
			client.refresh_sleep_state.side_effect = MetalClientError("connection refused")
			with self.assertRaises(MetalClientError):
				VirtualMachineSleepService(machine).reconcile()
			self.assertIsNone(cache.get_value(refresh_key, use_local_cache=False))
			client.refresh_sleep_state.side_effect = None
			VirtualMachineSleepService(machine).reconcile()
			self.assertEqual(client.refresh_sleep_state.call_count, 3)

			response["observed"]["sleep_snapshot"]["id"] = "checkpoint-2"
			client.get_virtual_machine.return_value = MetalVirtualMachine.from_dict(response)
			self.addCleanup(cache.delete_value, f"atlas:vm-sleep-urls:{machine.name}:checkpoint-2")
			VirtualMachineSleepService(machine).reconcile()
			self.assertEqual(client.refresh_sleep_state.call_count, 4)
			self.assertEqual(client.refresh_sleep_state.call_args.args[1]["snapshot_id"], "checkpoint-2")
			client.set_virtual_machine_power_state.assert_not_called()

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
			("comatose", 7200, []),
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
