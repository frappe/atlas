#!/usr/bin/env python3
# A tiny fake site VM for the proxy test harness (spec/12-proxy.md). Listens on
# [::]:80 (IPv6, plaintext) to mirror the real public-v6 site target, and echoes
# back its own name + the Host header it saw so the test can assert the proxy
# routed to the right upstream and preserved Host. Handles a /socket.io
# websocket upgrade (returns the 101 handshake) so the ws path can be exercised.
#
# The echo line ALSO reports the hop-by-hop / forwarded headers the proxy is
# supposed to inject (X-Forwarded-Proto/-For, X-Real-IP) and the Connection
# header it forwarded, so the behavior tests can assert the proxy adds them.
# Two extra debug endpoints support the timing/connection tests:
#   GET /__stream  — a chunked trickle (first byte now, rest after a delay) so a
#                    test can prove proxy_buffering off streams the first byte
#                    without waiting for the whole body.
#   GET /__conns   — the count of TCP connections this upstream has accepted, so
#                    a test can observe whether the proxy pools upstream
#                    connections (it doesn't today — Connection "" + no upstream
#                    keepalive block).

import base64
import hashlib
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

NAME = os.environ.get("UPSTREAM_NAME", "upstream")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic

# Connections accepted so far, bumped once per new TCP connection (see V6Server).
# A plain int guarded by a lock — the server is threaded.
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
		# Echo the forwarded headers the proxy injects (router.lua/nginx.conf), so
		# the behavior tests can assert their presence/values. Empty when absent.
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
		# Chunked: flush "A" immediately, sleep, then "B". A proxy that streams
		# (proxy_buffering off) delivers "A" to the client well before "B"; a
		# buffering proxy would withhold the whole body until the upstream is done.
		self.send_response(200)
		self.send_header("Content-Type", "text/plain")
		self.send_header("Transfer-Encoding", "chunked")
		self.end_headers()
		self._write_chunk(b"A")
		time.sleep(2.0)
		self._write_chunk(b"B")
		self.wfile.write(b"0\r\n\r\n")  # terminating chunk
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
		# Leave the connection open briefly so the proxy sees a live upgrade.

	def log_message(self, *args) -> None:  # quiet
		pass


class V6Server(socketserver.ThreadingTCPServer):
	address_family = socket.AF_INET6
	allow_reuse_address = True
	daemon_threads = True

	def get_request(self):
		# One bump per accepted TCP connection — the oracle for "did the proxy
		# open a new upstream connection per request, or reuse one?".
		global _conn_count
		conn = super().get_request()
		with _conn_lock:
			_conn_count += 1
		return conn


if __name__ == "__main__":
	with V6Server(("::", 80), Handler) as server:
		server.serve_forever()
