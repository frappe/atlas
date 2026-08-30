#!/usr/bin/env python3
# A deliberately-broken raw-TCP "service VM" for the L4 forwarder robustness tests
# (spec/17-tcp-proxy.md). The TCP analogue of misbehave.py: it is a SEPARATE script
# from tcp_upstream.py on purpose — tcp_upstream banners + echoes (a well-behaved
# backend), this one chooses a FAILURE MODE so the proxy's "one bad backend must
# never wedge the pipe" property can be proven.
#
# Unlike the HTTP misbehave.py (which picks its mode from the forwarded Host header),
# raw TCP carries no routing key the upstream can read, so the mode is fixed per
# CONTAINER via UPSTREAM_MODE. The compose file runs one container per mode and the
# test maps a port at whichever backend exercises the mode it wants.
#
# The property under test: a misbehaving backend must make the proxy fail the ONE
# affected connection cleanly (the client times out or sees an RST), and the
# forwarder must keep serving every OTHER mapped port — never crash, wedge, or pin a
# worker. No third-party deps.
#
# Modes (UPSTREAM_MODE):
#   accept-silent  — accept the connection, then never send a byte and never close.
#                    The client read blocks; proxy_timeout (1h) would eventually fire
#                    on the proxy, but the test bounds its own read and asserts no
#                    banner ever arrives (a silent backend yields no upstream data).
#   accept-rst     — accept, then immediately close (RST/FIN) without sending. The
#                    client connection drops with no banner.
#   slow-banner    — send ONE byte, then hang forever. Proves a partial-then-stall
#                    backend doesn't deliver a usable banner and doesn't wedge peers.

import os
import socket
import time

NAME = os.environ.get("UPSTREAM_NAME", "tcp-misbehave")
MODE = os.environ.get("UPSTREAM_MODE", "accept-rst")
PORT = int(os.environ.get("UPSTREAM_PORT", "7000"))


def handle(conn: socket.socket) -> None:
	try:
		if MODE == "accept-silent":
			# Accept and stall — never write, never close. Hold the socket so the
			# proxy sees an established-but-silent backend.
			while True:
				time.sleep(3600)
		elif MODE == "slow-banner":
			# A single byte, then stall — a partial banner that never completes.
			conn.sendall(b"u")
			while True:
				time.sleep(3600)
		else:  # accept-rst (default): drop immediately, no data.
			conn.close()
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
	srv.bind(("::", PORT))
	srv.listen(64)
	while True:
		conn, _ = srv.accept()
		# One thread per connection so accept-silent/slow-banner holds don't block the
		# accept loop (the proxy may open several probe connections).
		import threading

		threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
	main()
