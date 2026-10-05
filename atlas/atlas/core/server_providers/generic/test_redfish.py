from __future__ import annotations

from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerPowerAction
from atlas.atlas.core.server_providers.generic.redfish import RedfishClient, RedfishError

SYSTEM_URL = "https://bmc.local/redfish/v1/Systems/1"
REQUEST = "atlas.atlas.core.server_providers.generic.redfish.requests.request"


def system_response(reset: dict) -> Mock:
	response = Mock(status_code=200, content=b"{}")
	response.json.return_value = {"PowerState": "On", "Actions": {"#ComputerSystem.Reset": reset}}
	return response


class TestRedfishClient(UnitTestCase):
	def test_set_power_state_sends_the_first_reset_type_that_the_bmc_allows(self) -> None:
		reset = {
			"target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
			"ResetType@Redfish.AllowableValues": ["On", "ForceOff"],
		}

		with patch(
			REQUEST, side_effect=[system_response(reset), Mock(status_code=204, content=b"")]
		) as request:
			RedfishClient(SYSTEM_URL, "admin", "secret").set_power_state(ServerPowerAction.STOP)

		post = request.call_args_list[1]
		self.assertEqual(post.args, ("POST", f"{SYSTEM_URL}/Actions/ComputerSystem.Reset"))
		self.assertEqual(post.kwargs["json"], {"ResetType": "ForceOff"})

	def test_set_power_state_refuses_a_reset_target_on_another_host(self) -> None:
		reset = {"target": "https://attacker.example/reset"}

		with (
			patch(REQUEST, return_value=system_response(reset)) as request,
			self.assertRaises(RedfishError),
		):
			RedfishClient(SYSTEM_URL, "admin", "secret").set_power_state(ServerPowerAction.REBOOT)

		self.assertEqual(request.call_count, 1)

	def test_only_a_failed_read_is_retryable(self) -> None:
		client = RedfishClient(SYSTEM_URL, "admin", "secret")

		with patch(REQUEST, side_effect=requests.ConnectionError):
			with self.assertRaises(RedfishError) as read:
				client.request("GET", SYSTEM_URL)
			with self.assertRaises(RedfishError) as action:
				client.request("POST", SYSTEM_URL, json={"ResetType": "On"})

		self.assertTrue(read.exception.is_retryable)
		self.assertFalse(action.exception.is_retryable)

	def test_request_reports_the_redfish_error_message(self) -> None:
		response = Mock(status_code=401, content=b"{}")
		response.json.return_value = {"error": {"message": "Authorization required"}}

		with patch(REQUEST, return_value=response), self.assertRaises(RedfishError) as raised:
			RedfishClient(SYSTEM_URL, "admin", "wrong").read_power_state()

		self.assertIn("status 401: Authorization required", str(raised.exception))
		self.assertFalse(raised.exception.is_retryable)
