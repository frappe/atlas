from __future__ import annotations

from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.generic.bmc.client import RedfishClient, RedfishError

SYSTEM_URL = "http://bmc.example/redfish/v1/Systems/host-1"
RESET_URL = SYSTEM_URL + "/Actions/ComputerSystem.Reset"
TASK_URL = "http://bmc.example/redfish/v1/TaskService/TaskMonitors/1"
MODULE = "atlas.atlas.core.server_providers.generic.bmc.client"


def resource(power_state: str, reset_types: tuple[str, ...] = ("On",)) -> dict:
	return {
		"@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
		"@odata.id": SYSTEM_URL,
		"PowerState": power_state,
		"Actions": {
			"#ComputerSystem.Reset": {
				"target": RESET_URL,
				"ResetType@Redfish.AllowableValues": list(reset_types),
			}
		},
	}


def response(payload: object = None, status: int = 200, headers: dict | None = None) -> Mock:
	return Mock(
		status_code=status,
		headers=headers or {},
		content=b"{}" if payload is not None else b"",
		json=Mock(return_value=payload),
	)


class TestRedfishPowerOn(UnitTestCase):
	def test_power_on_uses_the_advertised_target_and_observes_the_transition(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(resource("Off")),
					response(resource("PoweringOn")),
					response(resource("On")),
				],
			),
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.sleep"),
		):
			RedfishClient(SYSTEM_URL).power_on()
		post.assert_called_once_with(
			RESET_URL,
			auth=None,
			headers={"Accept": "application/json"},
			json={"ResetType": "On"},
			timeout=10,
			allow_redirects=False,
		)

	def test_power_on_is_idempotent_when_already_on(self) -> None:
		with patch("requests.get", return_value=response(resource("On"))), patch("requests.post") as post:
			RedfishClient(SYSTEM_URL).power_on()
		post.assert_not_called()

	def test_force_on_is_used_only_when_advertised(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[response(resource("Off", ("ForceOn",))), response(resource("On"))],
			),
			patch("requests.post", return_value=response(status=204)) as post,
		):
			RedfishClient(SYSTEM_URL).power_on()
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "ForceOn"})

	def test_reads_allowable_values_from_action_info(self) -> None:
		system = resource("Off")
		system["Actions"]["#ComputerSystem.Reset"] = {
			"target": RESET_URL,
			"@Redfish.ActionInfo": SYSTEM_URL + "/ResetActionInfo",
		}
		information = {"Parameters": [{"Name": "ResetType", "AllowableValues": ["On"]}]}
		with (
			patch(
				"requests.get",
				side_effect=[response(system), response(information), response(resource("On"))],
			) as read,
			patch("requests.post", return_value=response(status=204)),
		):
			RedfishClient(SYSTEM_URL).power_on()
		self.assertEqual(read.call_args_list[1].args[0], SYSTEM_URL + "/ResetActionInfo")

	def test_refuses_unsafe_or_unsupported_reset_parameters_before_posting(self) -> None:
		for action in (
			{"target": "http://other.example/redfish/v1/reset", "ResetType@Redfish.AllowableValues": ["On"]},
			{"target": RESET_URL},
			{"target": RESET_URL, "ResetType@Redfish.AllowableValues": ["ForceOff"]},
			{"target": RESET_URL, "ResetType@Redfish.AllowableValues": [42]},
		):
			system = {**resource("Off"), "Actions": {"#ComputerSystem.Reset": action}}
			with (
				self.subTest(action=action),
				patch("requests.get", return_value=response(system)),
				patch("requests.post") as post,
			):
				with self.assertRaises(RedfishError):
					RedfishClient(SYSTEM_URL).power_on()
				post.assert_not_called()

	def test_transitional_power_state_refuses_another_reset(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("PoweringOff"))),
			patch("requests.post") as post,
		):
			with self.assertRaisesRegex(RedfishError, "stable"):
				RedfishClient(SYSTEM_URL).power_on()
		post.assert_not_called()

	def test_post_errors_are_never_retried(self) -> None:
		for result in (
			response(status=302),
			response(status=401),
			response(status=500),
			requests.ReadTimeout("test-password"),
		):
			with (
				self.subTest(result=result),
				patch("requests.get", return_value=response(resource("Off"))),
				patch(
					"requests.post",
					side_effect=result if isinstance(result, Exception) else None,
					return_value=result,
				) as post,
			):
				with self.assertRaises(RedfishError) as raised:
					RedfishClient(SYSTEM_URL).power_on()
				self.assertFalse(raised.exception.is_retryable)
				self.assertNotIn("test-password", str(raised.exception))
				post.assert_called_once()

	def test_waits_for_the_task_monitor_before_observing_power(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(resource("Off")),
					response({"TaskState": "Running"}, 202),
					response(status=204),
					response(resource("On")),
				],
			) as read,
			patch(
				"requests.post",
				return_value=response(
					{"TaskState": "Running"}, 202, {"Location": TASK_URL, "Retry-After": "3"}
				),
			) as post,
			patch(f"{MODULE}.sleep") as sleep,
		):
			RedfishClient(SYSTEM_URL).power_on()
		self.assertEqual(
			[call.args[0] for call in read.call_args_list], [SYSTEM_URL, TASK_URL, TASK_URL, SYSTEM_URL]
		)
		self.assertEqual(sleep.call_args_list[0].args[0], 3)
		post.assert_called_once()

	def test_waits_for_a_running_task_resource_that_returns_http_200(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(resource("Off")),
					response({"TaskState": "Running"}),
					response({"TaskState": "Completed", "TaskStatus": "OK"}),
					response(resource("On")),
				],
			),
			patch("requests.post", return_value=response(status=202, headers={"Location": TASK_URL})),
			patch(f"{MODULE}.sleep"),
		):
			RedfishClient(SYSTEM_URL).power_on()

	def test_missing_or_foreign_task_locations_do_not_forward_credentials(self) -> None:
		for location in (None, "http://other.example/redfish/v1/TaskService/Tasks/1"):
			with (
				patch("requests.get", return_value=response(resource("Off"))) as read,
				patch("requests.post", return_value=response(status=202, headers={"Location": location})),
			):
				with self.assertRaises(RedfishError):
					RedfishClient(SYSTEM_URL, "operator", "test-password").power_on()
				read.assert_called_once()

	def test_failed_tasks_are_not_reported_as_success(self) -> None:
		for task in (
			{"TaskState": "Exception"},
			{"TaskState": "Cancelled"},
			{"TaskState": "Completed", "TaskStatus": "Critical"},
			{"error": {"message": "private detail"}},
			[],
		):
			with (
				self.subTest(task=task),
				patch("requests.get", side_effect=[response(resource("Off")), response(task)]),
				patch("requests.post", return_value=response(status=202, headers={"Location": TASK_URL})),
				patch(f"{MODULE}.sleep"),
			):
				with self.assertRaisesRegex(RedfishError, "task failed"):
					RedfishClient(SYSTEM_URL).power_on()

	def test_power_transition_timeout_does_not_repeat_the_reset(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("Off"))),
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.monotonic", side_effect=[0, 0, 121, 121]),
			patch(f"{MODULE}.sleep"),
		):
			with self.assertRaisesRegex(RedfishError, "did not report On"):
				RedfishClient(SYSTEM_URL).power_on()
		post.assert_called_once()

	def test_task_timeout_does_not_repeat_the_reset(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("Off"))),
			patch("requests.post", return_value=response(status=202, headers={"Location": TASK_URL})) as post,
			patch(f"{MODULE}.monotonic", side_effect=[0, 0, 0, 121]),
			patch(f"{MODULE}.sleep"),
		):
			with self.assertRaisesRegex(RedfishError, "task did not finish"):
				RedfishClient(SYSTEM_URL).power_on()
		post.assert_called_once()


class TestRedfishPowerOff(UnitTestCase):
	def test_shutdown_returns_after_acceptance_without_power_or_task_polling(self) -> None:
		for status in (200, 202, 204):
			with (
				self.subTest(status=status),
				patch(
					"requests.get",
					return_value=response(resource("On", ("GracefulShutdown", "ForceOff"))),
				) as read,
				patch(
					"requests.post",
					return_value=response(
						{"TaskState": "Running"}, status=status, headers={"Location": TASK_URL}
					),
				) as post,
				patch(f"{MODULE}.sleep") as sleep,
			):
				RedfishClient(SYSTEM_URL).power_off()
			read.assert_called_once()
			post.assert_called_once()
			sleep.assert_not_called()
			self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "GracefulShutdown"})

	def test_already_off_does_not_send_a_reset(self) -> None:
		with patch("requests.get", return_value=response(resource("Off"))), patch("requests.post") as post:
			RedfishClient(SYSTEM_URL).power_off()
		post.assert_not_called()

	def test_force_off_only_is_refused(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("On", ("ForceOff",)))),
			patch("requests.post") as post,
		):
			with self.assertRaisesRegex(RedfishError, "GracefulShutdown"):
				RedfishClient(SYSTEM_URL).power_off()
		post.assert_not_called()

	def test_failed_shutdown_never_falls_back_to_force_off(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("On", ("GracefulShutdown", "ForceOff")))),
			patch("requests.post", return_value=response(status=500)) as post,
		):
			with self.assertRaisesRegex(RedfishError, "HTTP 500"):
				RedfishClient(SYSTEM_URL).power_off()
		post.assert_called_once()
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "GracefulShutdown"})


class TestRedfishReboot(UnitTestCase):
	def test_reboot_posts_even_when_already_on_and_observes_on(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(resource("On", ("GracefulRestart", "ForceRestart"))),
					response(resource("PoweringOff")),
					response(resource("On")),
				],
			),
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.sleep"),
		):
			RedfishClient(SYSTEM_URL).reboot()
		post.assert_called_once()
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "GracefulRestart"})

	def test_off_and_transitional_states_refuse_reboot(self) -> None:
		for state in ("Off", "PoweringOn", "PoweringOff"):
			with (
				self.subTest(state=state),
				patch("requests.get", return_value=response(resource(state, ("GracefulRestart",)))),
				patch("requests.post") as post,
			):
				with self.assertRaisesRegex(RedfishError, "reboot requires On"):
					RedfishClient(SYSTEM_URL).reboot()
				post.assert_not_called()

	def test_force_restart_only_is_refused(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("On", ("ForceRestart",)))),
			patch("requests.post") as post,
		):
			with self.assertRaisesRegex(RedfishError, "GracefulRestart"):
				RedfishClient(SYSTEM_URL).reboot()
		post.assert_not_called()

	def test_async_reboot_waits_for_completion_even_if_power_stays_on(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(resource("On", ("GracefulRestart",))),
					response({"TaskState": "Completed"}),
					response(resource("On")),
				],
			) as read,
			patch("requests.post", return_value=response(status=202, headers={"Location": TASK_URL})) as post,
			patch(f"{MODULE}.sleep"),
		):
			RedfishClient(SYSTEM_URL).reboot()
		self.assertEqual([call.args[0] for call in read.call_args_list], [SYSTEM_URL, TASK_URL, SYSTEM_URL])
		post.assert_called_once()


class TestRedfishPowerReadRecovery(UnitTestCase):
	def test_temporary_power_reads_recover_without_repeating_the_reset(self) -> None:
		for method, current, expected, reset_type in (
			("power_on", "Off", "On", "On"),
			("reboot", "On", "On", "GracefulRestart"),
		):
			for failure in (
				response({"error": "private detail"}, 500),
				response(status=502),
				response(status=503),
				response(status=504),
				requests.ConnectionError("test-password"),
				requests.ReadTimeout("test-password"),
			):
				with (
					self.subTest(method=method, failure=failure),
					patch(
						"requests.get",
						side_effect=[
							response(resource(current, (reset_type,))),
							failure,
							response(resource(expected)),
						],
					) as read,
					patch("requests.post", return_value=response(status=204)) as post,
					patch(f"{MODULE}.sleep") as sleep,
				):
					getattr(RedfishClient(SYSTEM_URL), method)()
					self.assertEqual(read.call_count, 3)
					sleep.assert_called_once()
					post.assert_called_once()
					self.assertEqual(post.call_args.kwargs["json"], {"ResetType": reset_type})

	def test_persistent_read_errors_stop_at_the_power_deadline(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[response(resource("Off", ("On",))), response(status=500)],
			) as read,
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.monotonic", side_effect=[0, 0, 121, 121]),
			patch(f"{MODULE}.sleep"),
		):
			with self.assertRaisesRegex(RedfishError, "last power read failed:.*HTTP 500.*refresh") as raised:
				RedfishClient(SYSTEM_URL).power_on()
		self.assertEqual(read.call_count, 2)
		post.assert_called_once()
		self.assertFalse(raised.exception.is_retryable)

	def test_permanent_read_failures_are_not_retried_after_reset(self) -> None:
		for failure in (
			response(status=302),
			response(status=401),
			response(status=403),
			response(status=404),
			requests.exceptions.SSLError("test-password"),
			response([]),
			response({}),
			response({**resource("Off"), "@odata.id": SYSTEM_URL + "-other"}),
		):
			with (
				self.subTest(failure=failure),
				patch(
					"requests.get",
					side_effect=[response(resource("Off", ("On",))), failure],
				) as read,
				patch("requests.post", return_value=response(status=204)) as post,
				patch(f"{MODULE}.sleep") as sleep,
			):
				with self.assertRaises(RedfishError) as raised:
					RedfishClient(SYSTEM_URL).power_on()
				self.assertNotIn("test-password", str(raised.exception))
				self.assertEqual(read.call_count, 2)
				sleep.assert_not_called()
				post.assert_called_once()

	def test_an_initial_read_failure_prevents_reset_without_retrying(self) -> None:
		with (
			patch("requests.get", return_value=response(status=500)) as read,
			patch("requests.post") as post,
			patch(f"{MODULE}.sleep") as sleep,
		):
			with self.assertRaisesRegex(RedfishError, "HTTP 500") as raised:
				RedfishClient(SYSTEM_URL).power_off()
		read.assert_called_once()
		post.assert_not_called()
		sleep.assert_not_called()
		self.assertFalse(raised.exception.is_retryable)
