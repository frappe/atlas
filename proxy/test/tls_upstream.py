#!/usr/bin/env python3
# A fake CUSTOM-DOMAIN site VM for the SNI-passthrough proxy test (spec/12 § The
# stream front-door, spec/18 Phase 2). Unlike upstream.py (a plaintext :80 site the
# proxy terminates TLS for), this VM TERMINATES ITS OWN TLS on :443 with a self-signed
# cert — exactly the trust boundary of a real custom domain, where the proxy passes the
# raw TLS stream through and the backend holds the key.
#
# Two listeners, mirroring a real bench VM behind the proxy's :443 SNI fork + :80 ACME
# fork:
#   :443 (TLS, own cert)  — echoes "upstream=<name> sni=<negotiated SNI> tls=backend",
#                           so the test can prove (a) it reached THIS backend and (b) the
#                           cert presented is the BACKEND's (CN=tls-vm), not the proxy's
#                           wildcard — i.e. the proxy never decrypted, it passed through.
#   :80  (plaintext)      — serves /.well-known/acme-challenge/<token> from an in-memory
#                           store (the VM completing its own HTTP-01), and echoes its name
#                           otherwise. The proxy's :80 ACME fork forwards a custom domain's
#                           challenge here; a wildcard challenge it answers itself.
#
# The self-signed cert's CN is "tls-vm.custom.example" (the test custom domain) so a
# client doing SNI for that name gets a cert that matches it — proving the backend, not
# the proxy, terminated.

import os
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

NAME = os.environ.get("UPSTREAM_NAME", "tls-vm")
CUSTOM_DOMAIN = os.environ.get("CUSTOM_DOMAIN", "tls-vm.custom.example")

# An in-memory ACME challenge store the :80 server answers from. A real VM writes these
# to a webroot when certbot runs; here a test seeds one over a tiny control endpoint.
_acme_tokens: dict[str, str] = {}
_acme_lock = threading.Lock()

# The negotiated SNI per TLS connection, keyed by the accepted socket's fileno so the
# handler can report which name the client asked for (the proof the SNI survived the
# proxy's passthrough). Set by the servername callback at handshake, read+popped once.
_sni_by_fileno: dict[int, str] = {}
_sni_lock = threading.Lock()


def _make_self_signed_cert() -> tuple[str, str]:
	"""Generate a self-signed cert+key for CUSTOM_DOMAIN, returning (cert_path, key_path).
	openssl is in the base image; this is the VM's OWN cert (the proxy never sees the key)."""
	tmp = tempfile.mkdtemp()
	cert = os.path.join(tmp, "fullchain.pem")
	key = os.path.join(tmp, "privkey.pem")
	subprocess.run(
		[
			"openssl",
			"req",
			"-x509",
			"-newkey",
			"rsa:2048",
			"-nodes",
			"-days",
			"3650",
			"-keyout",
			key,
			"-out",
			cert,
			"-subj",
			f"/CN={CUSTOM_DOMAIN}",
			"-addext",
			f"subjectAltName=DNS:{CUSTOM_DOMAIN}",
		],
		check=True,
		capture_output=True,
	)
	return cert, key


class _Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"

	def do_GET(self) -> None:
		# Custom-domain ACME challenge: serve from the in-memory store (the VM's HTTP-01).
		if self.path.startswith("/.well-known/acme-challenge/"):
			token = self.path.rsplit("/", 1)[-1]
			with _acme_lock:
				value = _acme_tokens.get(token)
			if value is None:
				self._send(404, b"no such challenge\n")
				return
			self._send(200, value.encode())
			return
		# A control endpoint the test uses to seed a challenge (stands in for certbot
		# writing the webroot): GET /__seed/<token>/<value>.
		if self.path.startswith("/__seed/"):
			_, _, rest = self.path.partition("/__seed/")
			token, _, value = rest.partition("/")
			with _acme_lock:
				_acme_tokens[token] = value
			self._send(200, b"seeded\n")
			return
		sni = ""
		if isinstance(self.connection, ssl.SSLSocket):
			with _sni_lock:
				sni = _sni_by_fileno.pop(self.connection.fileno(), "")
		tls = "backend" if isinstance(self.connection, ssl.SSLSocket) else "plain"
		host = self.headers.get("Host", "")
		self._send(200, f"upstream={NAME} sni={sni} tls={tls} host={host} path={self.path}\n".encode())

	def _send(self, status: int, body: bytes) -> None:
		self.send_response(status)
		self.send_header("Content-Type", "text/plain")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, *args) -> None:
		pass


class _V6Server(socketserver.ThreadingTCPServer):
	address_family = socket.AF_INET6
	allow_reuse_address = True
	daemon_threads = True


class _TLSV6Server(_V6Server):
	"""TLS server that terminates with its own cert and records the negotiated SNI per
	connection (keyed by fileno) so the handler can echo it back."""

	def __init__(self, addr, handler, context):
		super().__init__(addr, handler)
		self.ssl_context = context

	def get_request(self):
		sock, addr = super().get_request()
		tls_sock = self.ssl_context.wrap_socket(sock, server_side=True)
		# The servername callback (set on the context) stored the SNI keyed by the raw
		# socket's fileno before the wrap; re-key it onto the wrapped socket's fileno so
		# the handler (which sees the wrapped socket) can find it.
		with _sni_lock:
			sni = _sni_by_fileno.pop(sock.fileno(), "")
			_sni_by_fileno[tls_sock.fileno()] = sni
		return tls_sock, addr


def _run_tls() -> None:
	cert, key = _make_self_signed_cert()
	context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
	context.load_cert_chain(cert, key)

	def _sni_cb(ssl_sock, server_name, ctx):
		# Fires DURING the handshake (before wrap_socket returns). Key the SNI by the raw
		# socket's fileno; get_request re-keys it onto the wrapped socket.
		with _sni_lock:
			_sni_by_fileno[ssl_sock.fileno()] = server_name or ""

	context.set_servername_callback(_sni_cb)
	server = _TLSV6Server(("::", 443), _Handler, context)
	server.serve_forever()


def _run_plain() -> None:
	server = _V6Server(("::", 80), _Handler)
	server.serve_forever()


if __name__ == "__main__":
	t = threading.Thread(target=_run_plain, daemon=True)
	t.start()
	_run_tls()
