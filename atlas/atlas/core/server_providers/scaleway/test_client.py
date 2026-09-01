from __future__ import annotations

from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.scaleway import ScalewayError
from atlas.atlas.core.server_providers.scaleway.client import ScalewayClient


class TestScalewayClient(UnitTestCase):
	def test_request_includes_the_authentication_header(self) -> None:
		response = Mock(status_code=200, content=b"{}")
		response.json.return_value = {"id": "server-id"}

		with patch(
			"atlas.atlas.core.server_providers.scaleway.client.requests.request", return_value=response
		) as request:
			result = ScalewayClient("secret-key").request("GET", "/servers")

		self.assertEqual(result, {"id": "server-id"})
		self.assertEqual(request.call_args.args, ("GET", "https://api.scaleway.com/servers"))
		self.assertEqual(request.call_args.kwargs["headers"], {"X-Auth-Token": "secret-key"})

	def test_request_raises_for_an_unsuccessful_response(self) -> None:
		response = Mock(status_code=500, text="error")

		with (
			patch(
				"atlas.atlas.core.server_providers.scaleway.client.requests.request", return_value=response
			),
			self.assertRaises(ScalewayError),
		):
			ScalewayClient("secret-key").request("GET", "/servers")

	def test_request_rejects_a_non_object_json_response(self) -> None:
		response = Mock(status_code=200, content=b"[]")
		response.json.return_value = []

		with (
			patch(
				"atlas.atlas.core.server_providers.scaleway.client.requests.request", return_value=response
			),
			self.assertRaises(ScalewayError),
		):
			ScalewayClient("secret-key").request("GET", "/servers")
