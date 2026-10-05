from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.atlas.core.bmc_providers import BMCError, RedfishBMCProvider
from atlas.atlas.core.server_providers.base import ServerPowerAction

SYSTEM_URL = "https://bmc.example/redfish/v1/Systems/host-1"
RESET_URL = SYSTEM_URL + "/Actions/ComputerSystem.Reset"
TASK_URL = "https://bmc.example/redfish/v1/TaskService/TaskMonitors/1"
MODULE = "atlas.atlas.core.bmc_providers.redfish"


def system(power_state: str, reset_types: tuple[str, ...] = ("On",)) -> dict:
	return {
		"@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
		"@odata.id": SYSTEM_URL,
		"PowerState": power_state,
		"Status": {"Health": "OK"},
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


def redfish(url: str = SYSTEM_URL, username: str = "", password: str = "") -> RedfishBMCProvider:
	return RedfishBMCProvider(url, username, password)


class TestRedfishBMCProvider(UnitTestCase):
	def test_normalizes_the_system_url(self) -> None:
		self.assertEqual(redfish("HTTPS://BMC.EXAMPLE:443/redfish/v1/Systems/host-1/").url, SYSTEM_URL)

	def test_rejects_invalid_urls_and_incomplete_credentials(self) -> None:
		for url in (
			"",
			"file:///redfish/v1",
			"https://bmc.example:bad/redfish/v1",
			"https://user:secret@bmc.example/redfish/v1",
			SYSTEM_URL + "?password=secret",
			"https://bmc.example/other",
		):
			with self.subTest(url=url), self.assertRaises(BMCError):
				redfish(url)
		with self.assertRaisesRegex(BMCError, "both"):
			redfish(username="operator")

	def test_allows_plain_http_only_in_developer_mode(self) -> None:
		url = SYSTEM_URL.replace("https", "http")
		with patch(f"{MODULE}.frappe.conf", SimpleNamespace(developer_mode=0)):
			with self.assertRaisesRegex(BMCError, "HTTPS"):
				redfish(url)
		with patch(f"{MODULE}.frappe.conf", SimpleNamespace(developer_mode=1)):
			self.assertEqual(redfish(url).url, url)

	def test_reads_power_status_of_the_configured_system_only(self) -> None:
		with patch("requests.get", return_value=response(system("On"))):
			status = redfish().read_power_status()
		self.assertEqual((status.power_state, status.health), ("On", "OK"))

		for payload in (
			{**system("On"), "@odata.id": "/redfish/v1/Systems/other"},
			{**system("On"), "PowerState": "Unknown"},
			{**system("On"), "Status": {"Health": 42}},
		):
			with self.subTest(payload=payload), patch("requests.get", return_value=response(payload)):
				with self.assertRaises(BMCError):
					redfish().read_power_status()

	def test_power_on_uses_the_advertised_reset_and_waits_for_on(self) -> None:
		with (
			patch(
				"requests.get", side_effect=[response(system("Off", ("ForceOn",))), response(system("On"))]
			),
			patch("requests.post", return_value=response(status=204)) as post,
		):
			redfish("https://bmc.example/redfish/v1/Systems/host-1", "operator", "secret").set_power_state(
				ServerPowerAction.START
			)
		self.assertEqual(post.call_args.args[0], RESET_URL)
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "ForceOn"})
		self.assertEqual(post.call_args.kwargs["auth"], ("operator", "secret"))

	def test_reads_allowable_reset_values_from_action_info(self) -> None:
		payload = system("Off")
		payload["Actions"]["#ComputerSystem.Reset"] = {
			"target": RESET_URL,
			"@Redfish.ActionInfo": SYSTEM_URL + "/ResetActionInfo",
		}
		information = {"Parameters": [{"Name": "ResetType", "AllowableValues": ["On"]}]}
		with (
			patch(
				"requests.get", side_effect=[response(payload), response(information), response(system("On"))]
			),
			patch("requests.post", return_value=response(status=204)) as post,
		):
			redfish().set_power_state(ServerPowerAction.START)
		post.assert_called_once()

	def test_refuses_unsafe_or_unsupported_resets_before_posting(self) -> None:
		cases = [
			(system("PoweringOff"), ServerPowerAction.START),
			(system("Off", ("GracefulRestart",)), ServerPowerAction.REBOOT),
			(system("On", ("ForceOff",)), ServerPowerAction.STOP),
			(system("On", ("ForceRestart",)), ServerPowerAction.REBOOT),
			(
				{
					**system("Off"),
					"Actions": {"#ComputerSystem.Reset": {"target": "https://other.example/reset"}},
				},
				ServerPowerAction.START,
			),
		]
		for payload, action in cases:
			with (
				self.subTest(action=action, payload=payload),
				patch("requests.get", return_value=response(payload)),
				patch("requests.post") as post,
			):
				with self.assertRaises(BMCError):
					redfish().set_power_state(action)
				post.assert_not_called()

	def test_shutdown_returns_after_acceptance_without_polling(self) -> None:
		with (
			patch(
				"requests.get", return_value=response(system("On", ("GracefulShutdown", "ForceOff")))
			) as read,
			patch("requests.post", return_value=response(status=202, headers={"Location": TASK_URL})) as post,
			patch(f"{MODULE}.sleep") as sleep,
		):
			redfish().set_power_state(ServerPowerAction.STOP)
		read.assert_called_once()
		sleep.assert_not_called()
		self.assertEqual(post.call_args.kwargs["json"], {"ResetType": "GracefulShutdown"})

	def test_skips_the_reset_when_the_power_state_already_matches(self) -> None:
		for power_state, action in (("On", ServerPowerAction.START), ("Off", ServerPowerAction.STOP)):
			with (
				self.subTest(action=action),
				patch("requests.get", return_value=response(system(power_state))),
				patch("requests.post") as post,
			):
				redfish().set_power_state(action)
			post.assert_not_called()

	def test_unknown_reset_outcome_is_not_retried(self) -> None:
		with (
			patch("requests.get", return_value=response(system("On", ("GracefulRestart",)))),
			patch("requests.post", side_effect=requests.ReadTimeout) as post,
		):
			with self.assertRaisesRegex(BMCError, "outcome is unknown"):
				redfish().set_power_state(ServerPowerAction.REBOOT)
		post.assert_called_once()

	def test_reboot_follows_the_task_monitor_before_observing_power(self) -> None:
		with (
			patch(
				"requests.get",
				side_effect=[
					response(system("On", ("GracefulRestart",))),
					response({"TaskState": "Running"}, 202),
					response({"TaskState": "Completed", "TaskStatus": "OK"}),
					response(system("On")),
				],
			) as read,
			patch("requests.post", return_value=response(status=202, headers={"Location": TASK_URL})) as post,
			patch(f"{MODULE}.sleep"),
		):
			redfish().set_power_state(ServerPowerAction.REBOOT)
		self.assertEqual([call.args[0] for call in read.call_args_list[1:3]], [TASK_URL, TASK_URL])
		post.assert_called_once()

	def test_failed_or_foreign_tasks_are_not_reported_as_success(self) -> None:
		for task, location in (
			({"TaskState": "Exception"}, TASK_URL),
			({"TaskState": "Completed", "TaskStatus": "Critical"}, TASK_URL),
			({"TaskState": "Running"}, "https://other.example/redfish/v1/TaskService/Tasks/1"),
		):
			with (
				self.subTest(task=task, location=location),
				patch("requests.get", side_effect=[response(system("Off")), response(task)]),
				patch("requests.post", return_value=response(status=202, headers={"Location": location})),
				patch(f"{MODULE}.sleep"),
			):
				with self.assertRaises(BMCError):
					redfish().set_power_state(ServerPowerAction.START)

	def test_power_timeout_does_not_repeat_the_reset(self) -> None:
		with (
			patch("requests.get", return_value=response(system("Off"))),
			patch("requests.post", return_value=response(status=204)) as post,
			patch(f"{MODULE}.monotonic", side_effect=[0, 0, 121, 121]),
			patch(f"{MODULE}.sleep"),
		):
			with self.assertRaisesRegex(BMCError, "did not report On"):
				redfish().set_power_state(ServerPowerAction.START)
		post.assert_called_once()
