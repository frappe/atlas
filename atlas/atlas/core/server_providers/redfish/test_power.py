from __future__ import annotations

from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.redfish.client import RedfishClient, RedfishError

SYSTEM_URL = "http://bmc.example/redfish/v1/Systems/host-1"
RESET_URL = SYSTEM_URL + "/Actions/ComputerSystem.Reset"
TASK_URL = "http://bmc.example/redfish/v1/TaskService/TaskMonitors/1"
MODULE = "atlas.atlas.core.server_providers.redfish.client"


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
	def test_graceful_shutdown_observes_off(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(resource("On", ("GracefulShutdown", "ForceOff"))),
					response(resource("PoweringOff")),
					response(resource("Off")),
				],
			),
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.sleep"),
		):
			RedfishClient(SYSTEM_URL).power_off()
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "GracefulShutdown"})
		post.assert_called_once()

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

	def test_shutdown_timeout_never_falls_back_to_force_off(self) -> None:
		with (
			patch("requests.get", return_value=response(resource("On", ("GracefulShutdown", "ForceOff")))),
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.monotonic", side_effect=[0, 0, 121, 121]),
			patch(f"{MODULE}.sleep"),
		):
			with self.assertRaisesRegex(RedfishError, "did not report Off"):
				RedfishClient(SYSTEM_URL).power_off()
		post.assert_called_once()
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "GracefulShutdown"})
