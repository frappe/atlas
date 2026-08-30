#!/usr/bin/env python3
# A deliberately-broken "site VM" for the proxy robustness tests. It is a SEPARATE
# script from upstream.py on purpose: it must NOT speak valid HTTP, so it can't
# share the http.server-based good upstream. Listens on [::]:80 (plaintext v6),
# the same target shape as a real site, and replies with garbage or a truncated
# response depending on the Host header — so one container serves both failure
# modes and a test picks the mode by which subdomain it maps to it.
#
# The property under test: a misbehaving upstream must make the proxy return a
# clean gateway error (502) or close cleanly, NEVER crash, wedge, or pass garbage
# through as a 200. nginx parses the upstream response; a non-HTTP or truncated
# reply is an upstream protocol error → 502.
#
# Modes (matched on the Host header the proxy forwards, case-insensitive):
#   *garbage*    — write a non-HTTP blob and close. nginx → 502 (invalid header).
#   *truncated*  — promise Content-Length: 100, send 3 bytes, close. nginx sees
#                  the upstream close mid-body → the client read fails (curl
#                  returns non-zero; the proxy logs an upstream error).
#   anything else — same as garbage (safe default).

import socket
import threading


def handle(conn: socket.socket) -> None:
	try:
		data = conn.recv(65536)
		host = ""
		for line in data.split(b"\r\n"):
			if line.lower().startswith(b"host:"):
				host = line.split(b":", 1)[1].strip().lower().decode("latin1")
				break
		if "truncated" in host:
			# Valid status + a Content-Length we won't honor, then close early.
			conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nABC")
		else:
			# Not HTTP at all — no status line nginx can parse.
			conn.sendall(b"GARBAGE NOT HTTP\r\n\r\nstill garbage")
	except OSError:
		pass
	finally:
		try:
			conn.close()
		except OSError:
			pass


def main() -> None:
	srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
	srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	srv.bind(("::", 80))
	srv.listen(64)
	while True:
		conn, _ = srv.accept()
		threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
	main()
