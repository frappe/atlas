import socket

from gatewayd.apply import create_listener


def test_the_listener_accepts_ipv4_and_ipv6() -> None:
	listener = create_listener(0)
	port = listener.getsockname()[1]
	try:
		for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
			with socket.create_connection((address, port), timeout=2) as client:
				assert client.family == family
	finally:
		listener.close()
