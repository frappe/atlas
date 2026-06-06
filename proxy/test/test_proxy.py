#!/usr/bin/env python3
# Proxy image-level release gate (proxy-design.md §9). Drives the running
# docker-compose stack: PUT/POST mappings through the admin socket, make HTTPS
# requests with a forced Host/SNI, assert routing/remap/sync/restart/TLS/ws.
#
# Run the stack first:  docker compose up --build -d
# Then:                 python3 -m pytest test_proxy.py -v
# Teardown:             docker compose down -v
#
# Uses curl (admin socket + h2 + resolve override) rather than a Python HTTP
# client so we get unix-socket, --resolve, and --http2 with one tool the dev box
# already has — matching the proxy's own control transport (curl --unix-socket).

import json
import os
import subprocess
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_SOCK = "/run/atlas-proxy/admin.sock"  # inside the proxy container

HTTPS = "127.0.0.1:8443"
HTTP = "127.0.0.1:8080"
REGION = "test"
VM_A = "fd00:a71a:5::a"
VM_B = "fd00:a71a:5::b"


def admin(method: str, path: str, body: str | None = None) -> tuple[int, str]:
	"""curl the admin unix socket FROM INSIDE the proxy container (faithful to
	production: Atlas reaches it over SSH-to-the-guest, never a host mount).
	Returns (status, body)."""
	curl = [
		"curl",
		"-s",
		"-o",
		"-",
		"-w",
		"\n%{http_code}",
		"--unix-socket",
		ADMIN_SOCK,
		"-X",
		method,
	]
	if body is not None:
		# Pass the body via stdin to dodge argv quoting through `exec`.
		curl += ["--data-binary", "@-"]
	curl.append(f"http://localhost{path}")
	cmd = ["docker", "compose", "exec", "-T", "proxy", *curl]
	out = subprocess.run(cmd, cwd=HERE, input=body, capture_output=True, text=True, check=True).stdout
	payload, _, status = out.rpartition("\n")
	return int(status), payload


def fetch(
	subdomain: str,
	path: str = "/",
	scheme: str = "https",
	http2: bool = False,
	extra: list[str] | None = None,
) -> tuple[int, str, str]:
	"""curl the proxy with Host/SNI forced to <subdomain>.test.local.
	Returns (status, body, headers)."""
	host = f"{subdomain}.{REGION}.frappe.dev"
	target = HTTPS if scheme == "https" else HTTP
	ip, _, port = target.partition(":")
	# Dump headers to a temp file (-D) so stdout is the body alone; the status
	# code comes via -w on its own. Keeps body/headers/status cleanly separated
	# regardless of HTTP version or body content.
	marker = "\n@@STATUS@@"
	cmd = ["curl", "-sk", "-D", "/dev/stderr", "-w", marker + "%{http_code}"]
	if http2:
		cmd.append("--http2")
	# Map the wildcard host:port onto the local published port (sets SNI + Host).
	# The URL MUST carry the same port or --resolve won't key-match.
	cmd += ["--resolve", f"{host}:{port}:{ip}", f"{scheme}://{host}:{port}{path}"]
	if extra:
		cmd += extra
	res = subprocess.run(cmd, capture_output=True, text=True)
	body, _, status = res.stdout.rpartition(marker)
	return int(status or 0), body, res.stderr


@pytest.fixture(scope="module", autouse=True)
def clean_map():
	"""Each module run starts from a known empty map."""
	_wait_for_socket()
	admin("POST", "/sync", "{}")
	yield


def _wait_for_socket(timeout: float = 30.0) -> None:
	deadline = time.time() + timeout
	while time.time() < deadline:
		try:
			status, _ = admin("GET", "/healthz")
			if status == 200:
				return
		except subprocess.CalledProcessError:
			pass
		time.sleep(0.5)
	raise RuntimeError("proxy admin socket never came up")


# --- routing ---------------------------------------------------------------


def test_routing_preserves_host():
	admin("PUT", "/map/acme", VM_A)
	status, body, _ = fetch("acme")
	assert status == 200
	assert "upstream=vm-a" in body
	assert "host=acme.test.frappe.dev" in body  # Host preserved end-to-end


def test_multi_subdomain_one_vm():
	admin("PUT", "/map/acme", VM_A)
	admin("PUT", "/map/widgets", VM_A)
	for sub in ("acme", "widgets"):
		status, body, _ = fetch(sub)
		assert status == 200 and "upstream=vm-a" in body


# --- remap without reload --------------------------------------------------


def test_remap_no_reload():
	admin("PUT", "/map/acme", VM_A)
	assert "upstream=vm-a" in fetch("acme")[1]
	pid_before = _proxy_master_pid()
	admin("PUT", "/map/acme", VM_B)
	status, body, _ = fetch("acme")
	assert status == 200 and "upstream=vm-b" in body
	assert _proxy_master_pid() == pid_before  # nginx never reloaded


# --- unmapped --------------------------------------------------------------


def test_unmapped_serves_branded_404():
	admin("POST", "/sync", "{}")
	status, body, _ = fetch("nope")
	assert status == 404
	assert "isn't here" in body  # the branded page, no upstream contacted


# --- bulk /sync ------------------------------------------------------------


def test_bulk_sync_replaces_atomically():
	admin("PUT", "/map/stale", VM_A)
	desired = json.dumps({"acme": VM_A, "widgets": VM_B}, sort_keys=True, indent=2)
	admin("POST", "/sync", desired)
	# Added entries present, removed entry gone.
	assert "upstream=vm-a" in fetch("acme")[1]
	assert "upstream=vm-b" in fetch("widgets")[1]
	assert fetch("stale")[0] == 404


def test_get_map_is_canonical_json():
	admin("POST", "/sync", json.dumps({"b": VM_B, "a": VM_A}))
	_, live = admin("GET", "/map")
	expected = json.dumps({"a": VM_A, "b": VM_B}, sort_keys=True, indent=2) + "\n"
	assert live == expected  # byte-identical to the Atlas-side serialization


# --- restart reload (persistence) ------------------------------------------


def test_restart_reloads_from_mapjson():
	admin("POST", "/sync", json.dumps({"acme": VM_A}))
	admin("POST", "/dump")  # force the snapshot now
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	_wait_for_socket()
	# No admin calls after restart — the dict repopulated from map.json.
	status, body, _ = fetch("acme")
	assert status == 200 and "upstream=vm-a" in body


# --- HTTP -> HTTPS ---------------------------------------------------------


def test_http_redirects_to_https():
	status, _, headers = fetch("acme", scheme="http")
	assert status == 308
	assert "location: https://acme.test.frappe.dev" in headers.lower()


# --- HTTP/2 ----------------------------------------------------------------


def test_http2_negotiated():
	admin("PUT", "/map/acme", VM_A)
	status, _, headers = fetch("acme", http2=True)
	assert status == 200
	assert "http/2" in headers.lower().splitlines()[0]


# --- socket.io websocket upgrade -------------------------------------------


def test_socketio_upgrade():
	admin("PUT", "/map/acme", VM_A)
	# Websocket upgrade is an HTTP/1.1 mechanism — force h1.1 (h2 has no Upgrade).
	# --max-time bounds the wait: the 101 handshake arrives immediately, then the
	# upgraded connection stays open (nginx's 3600s ws read timeout), so without a
	# cap curl would block forever waiting on the tunnel. curl reports the status
	# it already received when the timer fires.
	status, _, headers = fetch(
		"acme",
		path="/socket.io/",
		scheme="https",
		extra=[
			"--http1.1",
			"--max-time",
			"5",
			"-H",
			"Connection: Upgrade",
			"-H",
			"Upgrade: websocket",
			"-H",
			"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
			"-H",
			"Sec-WebSocket-Version: 13",
		],
	)
	assert status == 101
	assert "upgrade: websocket" in headers.lower()


# --- helpers ---------------------------------------------------------------


def _proxy_master_pid() -> str:
	"""nginx master PID inside the proxy container — to prove no reload."""
	out = subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", "cat", "/run/atlas-proxy/nginx.pid"],
		cwd=HERE,
		capture_output=True,
		text=True,
		check=True,
	).stdout
	return out.strip()
