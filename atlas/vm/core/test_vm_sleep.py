from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import urlsplit

from frappe.tests import UnitTestCase

from atlas.atlas.object_storage import ObjectStorageClient
from atlas.vm.core.metal_client import MetalClient
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_service import VirtualMachineService
from atlas.vm.core.vm_sleep import VirtualMachineSleepService


class TestVirtualMachineSleepPolicy(UnitTestCase):
	def test_disabling_offload_keeps_remote_recovery_urls_fresh(self) -> None:
		storage = ObjectStorageClient(
			bucket="snapshots",
			access_key_id="test-access",
			secret_access_key="test-secret",
			endpoint_url="https://objects.example",
			region="test-region",
		)
		settings = SimpleNamespace(
			deep_sleep_after_idle_seconds=0,
			coma_after_idle_seconds=0,
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
		for state, artifacts in (
			("deep-sleeping", {"memory": {"uploaded": True}}),
			("comatose", {"memory": {"uploaded": True}, "disk": {"uploaded": True}}),
		):
			response = complete_response()
			response["observed"].update(
				state="stopped", sleep_state=state, sleep_snapshot={"id": "checkpoint-1", **artifacts}
			)
			client = Mock(spec=MetalClient)
			client.get_virtual_machine.return_value = MetalVirtualMachine.from_dict(response)
			with (
				self.subTest(state=state),
				patch.object(
					VirtualMachineService, "metal_client", new_callable=PropertyMock, return_value=client
				),
				patch("atlas.vm.core.vm_sleep.frappe.get_cached_doc", return_value=settings),
			):
				VirtualMachineSleepService(virtual_machine).reconcile()

			client.offload_sleep_state.assert_not_called()
			client.set_virtual_machine_power_state.assert_not_called()
			request = client.refresh_sleep_state.call_args.args[1]
			self.assertEqual(request.pop("snapshot_id"), "checkpoint-1")
			self.assertEqual(set(request), set(artifacts))
			for name, target in request.items():
				self.assertEqual(set(target), {"download_url"})
				self.assertTrue(urlsplit(target["download_url"]).path.endswith(f"/host-1/vm-1/{name}"))

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
