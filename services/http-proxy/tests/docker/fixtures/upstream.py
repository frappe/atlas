#!/usr/bin/env python3
"""A fake site VM for the test harness.

Listens on [::]:80 in plain text, like a real site target, and echoes its name,
the Host header and the headers the proxy injects. Answers a websocket upgrade on
/socket.io with the 101 handshake.

	GET /__stream   one byte, a pause, then the rest - proves the first byte
	                arrives early with `proxy_buffering off`.
	GET /__conns    the count of accepted connections - shows whether the proxy
	                pools them. Today it does not.
"""

import base64
import hashlib
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

NAME = os.environ.get("UPSTREAM_NAME", "upstream")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # the constant from RFC 6455

# The server uses one thread for each connection, thus a lock protects this count.
_conn_count = 0
_conn_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"

	def _is_websocket(self) -> bool:
		return (
			self.headers.get("Upgrade", "").lower() == "websocket"
			and "upgrade" in self.headers.get("Connection", "").lower()
		)

	def do_GET(self) -> None:
		if self.path == "/__conns":
			return self._serve_conns()
		if self.path == "/__stream":
			return self._serve_stream()
		if self.path.startswith("/socket.io") and self._is_websocket():
			return self._handshake_websocket()
		host = self.headers.get("Host", "")
		xfproto = self.headers.get("X-Forwarded-Proto", "")
		xff = self.headers.get("X-Forwarded-For", "")
		xrealip = self.headers.get("X-Real-IP", "")
		conn = self.headers.get("Connection", "")
		body = (
			f"upstream={NAME} host={host} path={self.path} "
			f"xfproto={xfproto} xff={xff} xrealip={xrealip} conn={conn}\n"
		).encode()
		self.send_response(200)
		self.send_header("Content-Type", "text/plain")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _serve_conns(self) -> None:
		with _conn_lock:
			n = _conn_count
		body = f'{{"conns": {n}}}\n'.encode()
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _serve_stream(self) -> None:
		"""Send "A", wait, then "B". A streaming proxy delivers "A" long first."""
		self.send_response(200)
		self.send_header("Content-Type", "text/plain")
		self.send_header("Transfer-Encoding", "chunked")
		self.end_headers()
		self._write_chunk(b"A")
		time.sleep(2.0)
		self._write_chunk(b"B")
		self.wfile.write(b"0\r\n\r\n")  # the final chunk
		self.wfile.flush()

	def _write_chunk(self, data: bytes) -> None:
		self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
		self.wfile.flush()

	def _handshake_websocket(self) -> None:
		key = self.headers.get("Sec-WebSocket-Key", "")
		accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
		self.send_response(101)
		self.send_header("Upgrade", "websocket")
		self.send_header("Connection", "Upgrade")
		self.send_header("Sec-WebSocket-Accept", accept)
		self.end_headers()

	def log_message(self, *args) -> None:
		pass


class V6Server(socketserver.ThreadingTCPServer):
	address_family = socket.AF_INET6
	allow_reuse_address = True
	daemon_threads = True

	def get_request(self):
		"""Count each accepted connection: a new one per request, or one reused?"""
		global _conn_count
		conn = super().get_request()
		with _conn_lock:
			_conn_count += 1
		return conn


if __name__ == "__main__":
	with V6Server(("::", 80), Handler) as server:
		server.serve_forever()
