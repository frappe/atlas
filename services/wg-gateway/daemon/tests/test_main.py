import ipaddress

from gatewayd.main import _client_fdac


def test_client_address_keeps_gateway_tenant_and_client_fields() -> None:
	address = ipaddress.IPv6Address(_client_fdac(2, 7, 42, 9))
	assert address == ipaddress.IPv6Address("fdac:2:7:0:2a:0:9:0")
	assert address in ipaddress.IPv6Network("fdac:2:7::/48")
	assert address not in ipaddress.IPv6Network("fdac:2:8::/48")
	assert _client_fdac(2, 7, 0x12345678, 0x9ABCDEF0) == "fdac:2:7:1234:5678:9abc:def0:0"
