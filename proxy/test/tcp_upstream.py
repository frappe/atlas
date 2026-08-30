#!/usr/bin/env python3
# A fake raw-TCP service VM for the proxy test harness (spec/17-tcp-proxy.md).
# Listens on [::]:7000 (IPv6, plaintext) to mirror a real tenant VM's service
# port reached over the public-v6 south hop. On every connection it sends a
# one-line banner identifying itself ("upstream=<name>\n") — like the HTTP
# upstream's body — so the TCP test can assert WHICH backend a forwarded port
# reached (and that a remap repointed it) — then echoes back whatever the client
# sends, so a round-trip byte test is meaningful. No third-party deps.

import os
import socket
import socketserver

NAME = os.environ.get("UPSTREAM_NAME", "tcp-upstream")
PORT = int(os.environ.get("UPSTREAM_PORT", "7000"))


class Handler(socketserver.BaseRequestHandler):
	def handle(self) -> None:
		# Identify ourselves immediately so a connect-only test can read the
		# banner and know which backend the proxy dialed.
		self.request.sendall(f"upstream={NAME}\n".encode())
		# Then echo: read what the client sends and send it straight back, so the
		# test can prove bytes round-trip through the L4 forwarder unchanged.
		while True:
			data = self.request.recv(4096)
			if not data:
				return
			self.request.sendall(data)


class V6Server(socketserver.ThreadingTCPServer):
	address_family = socket.AF_INET6
	allow_reuse_address = True
	daemon_threads = True


if __name__ == "__main__":
	with V6Server(("::", PORT), Handler) as server:
		server.serve_forever()
