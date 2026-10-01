from unittest import TestCase

from atlas.vm.core.models import ROUTE_SCOPE_WIREGUARD_GATEWAY, Route


class TestRouteScope(TestCase):
	def test_a_normal_route_carries_no_scope(self) -> None:
		route = Route.from_value({"destination": "2000::/3", "via": "host"})

		self.assertEqual(route, Route("2000::/3", "host"))
		self.assertEqual(route.as_dict(), {"destination": "2000::/3", "via": "host"})
		self.assertFalse(route.is_wireguard_gateway)

	def test_a_wireguard_gateway_route_keeps_its_scope(self) -> None:
		route = Route.from_value(
			{"destination": "fdac:1:1::/48", "via": "fdaa:1::1", "scope": ROUTE_SCOPE_WIREGUARD_GATEWAY}
		)

		self.assertEqual(route, Route("fdac:1:1::/48", "fdaa:1::1", ROUTE_SCOPE_WIREGUARD_GATEWAY))
		self.assertEqual(
			route.as_dict(),
			{
				"destination": "fdac:1:1::/48",
				"via": "fdaa:1::1",
				"scope": ROUTE_SCOPE_WIREGUARD_GATEWAY,
			},
		)
		self.assertTrue(route.is_wireguard_gateway)
		self.assertFalse(route.is_via_host)

	def parse_error(self, value: dict[str, str]) -> str:
		with self.assertRaises(ValueError) as error:
			Route.from_value(value)
		return str(error.exception)

	def test_an_unknown_scope_is_rejected(self) -> None:
		message = self.parse_error({"destination": "fdac:1:1::/48", "via": "fdaa:1::1", "scope": "tenant"})
		self.assertIn("wireguard-gateway", message)
