#!/usr/bin/env python3
# A tiny fake site VM for the proxy test harness (proxy-design.md §9). Listens on
# [::]:80 (IPv6, plaintext) to mirror the real public-v6 site target, and echoes
# back its own name + the Host header it saw so the test can assert the proxy
# routed to the right upstream and preserved Host. Handles a /socket.io
# websocket upgrade (returns the 101 handshake) so the ws path can be exercised.

import base64
import hashlib
import os
import socket
import socketserver
from http.server import BaseHTTPRequestHandler

NAME = os.environ.get("UPSTREAM_NAME", "upstream")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic


class Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"

	def _is_websocket(self) -> bool:
		return (
			self.headers.get("Upgrade", "").lower() == "websocket"
			and "upgrade" in self.headers.get("Connection", "").lower()
		)

	def do_GET(self) -> None:
		if self.path.startswith("/socket.io") and self._is_websocket():
			return self._handshake_websocket()
		host = self.headers.get("Host", "")
		body = f"upstream={NAME} host={host} path={self.path}\n".encode()
		self.send_response(200)
		self.send_header("Content-Type", "text/plain")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _handshake_websocket(self) -> None:
		key = self.headers.get("Sec-WebSocket-Key", "")
		accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
		self.send_response(101)
		self.send_header("Upgrade", "websocket")
		self.send_header("Connection", "Upgrade")
		self.send_header("Sec-WebSocket-Accept", accept)
		self.end_headers()
		# Leave the connection open briefly so the proxy sees a live upgrade.

	def log_message(self, *args) -> None:  # quiet
		pass


class V6Server(socketserver.ThreadingTCPServer):
	address_family = socket.AF_INET6
	allow_reuse_address = True
	daemon_threads = True


if __name__ == "__main__":
	with V6Server(("::", 80), Handler) as server:
		server.serve_forever()
