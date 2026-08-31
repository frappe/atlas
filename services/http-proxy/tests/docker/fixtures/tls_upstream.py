#!/usr/bin/env python3
"""A custom-domain site VM for the SNI passthrough test.

It terminates its OWN TLS on port 443 with a self-signed certificate whose CN is
the test custom domain. That is the trust boundary of a real custom domain: the
proxy passes the raw stream through and the backend holds the key.

	:443  echoes the name, the negotiated SNI and "tls=backend", thus a test can
	      prove which backend answered and whose certificate it presented.
	:80   answers /.well-known/acme-challenge/<token> from a store in memory,
	      like a VM completing its own HTTP-01 challenge.
"""

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

# A real VM writes these to a webroot when certbot runs. Here a test seeds one.
_acme_tokens: dict[str, str] = {}
_acme_lock = threading.Lock()

# Keyed by the file number of the socket, thus the handler can report the name the
# client asked for - the proof the SNI survived the passthrough.
_sni_by_fileno: dict[int, str] = {}
_sni_lock = threading.Lock()


def _make_self_signed_cert() -> tuple[str, str]:
    """The certificate of the VM. The proxy never sees the key."""
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
        if self.path.startswith("/.well-known/acme-challenge/"):
            token = self.path.rsplit("/", 1)[-1]
            with _acme_lock:
                value = _acme_tokens.get(token)
            if value is None:
                self._send(404, b"no such challenge\n")
                return
            self._send(200, value.encode())
            return
        # A control endpoint that a test uses to add a challenge. It takes the place
        # of certbot, which writes the webroot.
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
        self._send(
            200,
            f"upstream={NAME} sni={sni} tls={tls} host={host} path={self.path}\n".encode(),
        )

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
    """Terminates with its own certificate and records the SNI of each connection."""

    def __init__(self, addr, handler, context):
        super().__init__(addr, handler)
        self.ssl_context = context

    def get_request(self):
        sock, addr = super().get_request()
        # wrap_socket runs the handshake, thus the servername callback already keyed
        # the SNI by a file number the handler also sees. Do not re-key it here.
        tls_sock = self.ssl_context.wrap_socket(sock, server_side=True)
        return tls_sock, addr


def _run_tls() -> None:
    cert, key = _make_self_signed_cert()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def _sni_cb(ssl_sock, server_name, ctx):
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
