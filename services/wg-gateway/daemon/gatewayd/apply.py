import os
import socket

import uvicorn

from .config import ConfigError, load

LISTEN_PORT = 443


def main() -> None:
	try:
		config = load()
	except ConfigError as error:
		raise SystemExit(f"atlas-wg-gateway: {error}") from error

	tls_directory = config.state_directory / "tls"
	tls_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	certificate = tls_directory / "fullchain.pem"
	key = tls_directory / "privkey.pem"
	for path, content in ((certificate, config.certificate_pem), (key, config.private_key_pem)):
		descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(descriptor, "w") as target:
			target.write(content + "\n")

	server = uvicorn.Server(uvicorn.Config("gatewayd.main:app", ssl_certfile=certificate, ssl_keyfile=key))
	server.run(sockets=[create_listener(LISTEN_PORT)])


def create_listener(port: int) -> socket.socket:
	"""Listen on IPv4 and IPv6. Devices and the DNS health check reach the node over IPv4."""
	listener = socket.socket(socket.AF_INET6)
	listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
	listener.bind(("::", port))
	listener.listen(socket.SOMAXCONN)
	return listener


if __name__ == "__main__":
	main()
