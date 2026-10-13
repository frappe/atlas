from __future__ import annotations

import errno
import http.client
import os
import re
import shutil
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import frappe

from atlas.atlas.core.host_binaries import repository_path
from atlas.metal_server.core.atlas_peer import AtlasPeer

# Each side of the forwarder owns its own connection, so these headers stay on their side.
HOP_BY_HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"}
FORWARD_TIMEOUT_SECONDS = 300


class DevelopmentListenerError(Exception):
	"""The development listener cannot start."""


class DevelopmentListener:
	"""Serve atlas_internal_url on a development machine and forward each request to the local site.

	A development bench listens on 127.0.0.1 and finds the site from the Host header, so hosts cannot
	reach it directly.
	"""

	def __init__(self, site: str) -> None:
		self.site = site
		self.url = urlsplit(frappe.conf.get("atlas_internal_url") or "")
		self.web_port = int(frappe.conf.get("webserver_port") or 8000)
		self.firewall_rules_path = Path(f"/etc/atlas/atlas-wireguard-{AtlasPeer.interface}.nft")

	def check(self) -> None:
		"""Raise an error that gives the commands for the first missing setup step."""
		if not frappe.conf.get("developer_mode"):
			raise setup_error(
				"Turn on developer mode", f"pilot --site {self.site} set-config developer_mode 1"
			)
		if os.geteuid() == 0:
			raise DevelopmentListenerError("Run the development listener as your user, not as root.")

		atlas_peer = AtlasPeer()
		if not atlas_peer.settings.wireguard_ip_address or not atlas_peer.config_path.is_file():
			raise setup_error(
				"Create the Atlas WireGuard link", f"pilot --site {self.site} configure-atlas-wireguard"
			)
		if self.url.scheme != "http" or not self.url.hostname or not self.url.port:
			url = f"http://[{atlas_peer.settings.wireguard_ip_address}]:{self.web_port}"
			raise setup_error(
				"Set atlas_internal_url to the Atlas mesh address",
				f'pilot --site {self.site} set-config atlas_internal_url "{url}"',
			)

		install_command = (
			f'sudo ATLAS_WIREGUARD_TCP_PORTS="22, 80, 443, 2222, {self.url.port}" '
			f"{repository_path() / 'scripts' / 'install-atlas-wireguard.sh'} {atlas_peer.config_path}"
		)
		if not self.firewall_rules_path.is_file():
			raise setup_error(f"Install the {AtlasPeer.interface} link on this machine", install_command)
		if not is_port_allowed(self.firewall_rules_path.read_text(), self.url.port):
			raise setup_error(
				f"Open port {self.url.port} in the {AtlasPeer.interface} firewall", install_command
			)

		try:
			socket.create_connection(("127.0.0.1", self.web_port), timeout=1).close()
		except OSError as error:
			raise setup_error(
				f"Start the bench. Nothing listens on 127.0.0.1:{self.web_port}", "pilot start"
			) from error

	def serve(self) -> None:
		"""Forward requests until the process stops."""
		self.check()
		try:
			server = ForwardingServer(self.url.hostname, self.url.port, self.site, self.web_port)
		except OSError as error:
			if error.errno == errno.EADDRNOTAVAIL:
				raise setup_error(
					f"No interface has {self.url.hostname}. Apply the {AtlasPeer.interface} link again",
					f"sudo systemctl restart atlas-wireguard-{AtlasPeer.interface}.service",
				) from error
			raise

		with server:
			server.serve_forever()


def setup_error(step: str, *commands: str) -> DevelopmentListenerError:
	"""Return an error that names a setup step and the commands that do it."""
	return DevelopmentListenerError("\n".join([f"{step}. Run:", *(f"  {command}" for command in commands)]))


def is_port_allowed(firewall_rules: str, port: int) -> bool:
	"""Return whether the install-atlas-wireguard.sh rules accept a TCP port."""
	return any(
		str(port) in re.split(r"[\s,]+", ports)
		for ports in re.findall(r"tcp dport \{([^}]*)\}", firewall_rules)
	)


class ForwardingServer(ThreadingHTTPServer):
	"""Listen on one address and forward each request to the local site."""

	daemon_threads = True

	def __init__(self, address: str, port: int, site: str, web_port: int) -> None:
		self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
		self.site = site
		self.web_port = web_port
		super().__init__((address, port), ForwardingHandler)


class ForwardingHandler(BaseHTTPRequestHandler):
	"""Forward one request with the Host header of the site."""

	server: ForwardingServer

	def forward(self) -> None:
		# The forwarder passes a body by its length only, so it refuses a streamed one instead of dropping it.
		if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
			self.send_error(411, "Send the request body with a Content-Length")
			return

		length = int(self.headers.get("Content-Length") or 0)
		body = self.rfile.read(length) if length else None
		headers = {
			name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP_HEADERS
		}
		headers["Host"] = self.server.site

		connection = http.client.HTTPConnection(
			"127.0.0.1", self.server.web_port, timeout=FORWARD_TIMEOUT_SECONDS
		)
		try:
			connection.request(self.command, self.path, body, headers)
			response = connection.getresponse()

			# The site sends its own Date and Server headers.
			self.log_request(response.status)
			self.send_response_only(response.status, response.reason)
			for name, value in response.getheaders():
				if name.lower() not in HOP_BY_HOP_HEADERS:
					self.send_header(name, value)
			self.end_headers()
			shutil.copyfileobj(response, self.wfile)
		finally:
			connection.close()

	do_DELETE = do_GET = do_HEAD = do_PATCH = do_POST = do_PUT = forward
