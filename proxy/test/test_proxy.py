#!/usr/bin/env python3
# Proxy image-level release gate (spec/12-proxy.md). Drives the running
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
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_SOCK = "/run/nginx/admin.sock"  # inside the proxy container

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


def test_tombstone_serves_503():
	# router.lua §6.5: a known-but-suspended subdomain stores "-" and serves the
	# branded page with 503 ("preparing") rather than 404 ("no such site"). This is
	# a real router branch with no other coverage.
	admin("PUT", "/map/paused", "-")
	status, body, _ = fetch("paused")
	assert status == 503
	assert "isn't here" in body  # same branded page, different status


def test_no_region_suffix_serves_404():
	# A host that doesn't end in ".<region>.frappe.dev" has no derivable subdomain
	# under a configured region → branded 404, never a 500.
	admin("PUT", "/map/acme", VM_A)
	host = "acme.wrongregion.example.com"
	cmd = [
		"curl",
		"-sk",
		"-o",
		"/dev/null",
		"-w",
		"%{http_code}",
		"--resolve",
		f"{host}:8443:127.0.0.1",
		f"https://{host}:8443/",
	]
	status = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
	assert status == "404", status


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


# --- per-subdomain admin routes (GET/PUT/DELETE /map/<sub>) -----------------


def test_put_then_get_then_delete_single():
	# The per-subdomain CRUD the controller uses for incremental edits — each verb
	# has its own admin.lua branch and none was covered.
	status, _ = admin("PUT", "/map/solo", VM_A)
	assert status == 200
	status, body = admin("GET", "/map/solo")
	assert status == 200 and json.loads(body)["address"] == VM_A
	status, _ = admin("DELETE", "/map/solo")
	assert status == 200
	# Gone: both the admin lookup and the routed request 404.
	assert admin("GET", "/map/solo")[0] == 404
	assert fetch("solo")[0] == 404


def test_put_empty_body_rejected():
	# admin.lua rejects an empty address with 400 rather than mapping a blank.
	status, body = admin("PUT", "/map/blank", "")
	assert status == 400
	assert "empty" in body.lower()


def test_sync_malformed_body_rejected_without_corrupting_map():
	# A scalar, garbage, or a NON-EMPTY array must 400 and leave the live map
	# untouched. (cjson decodes a JSON array to a Lua table, so [1,2] would inject
	# numeric "subdomains" if admin.lua didn't validate entry types — it does.)
	# An empty array [] is accepted as "empty map" (a Lua empty table is
	# indistinguishable from {}), which is harmless, so it's excluded here.
	admin("PUT", "/map/keepme", VM_A)  # seed a known-good entry
	for bad in ('"x"', "42", "not json", "[1,2]", '["a","b"]'):
		status, _ = admin("POST", "/sync", bad)
		assert status == 400, f"{bad!r} unexpectedly accepted ({status})"
	# The seeded entry survived every rejected sync — no partial/garbage write.
	status, body = admin("GET", "/map/keepme")
	assert status == 200 and json.loads(body)["address"] == VM_A
	admin("POST", "/sync", "{}")  # reset for the next test


def test_unknown_admin_route_404s():
	status, body = admin("GET", "/nope")
	assert status == 404
	assert "unknown route" in body.lower()


# --- healthz ---------------------------------------------------------------


def test_healthz_reports_entries_and_last_dump():
	# §6.2: GET /healthz = nginx up + dict entry count + last-dump time.
	admin("POST", "/sync", json.dumps({"acme": VM_A, "widgets": VM_B}))
	admin("POST", "/dump")  # force a dump so last_dump is populated
	status, body = admin("GET", "/healthz")
	assert status == 200
	health = json.loads(body)
	assert health["ok"] is True
	assert health["entries"] == 2
	# last_dump is epoch seconds of the most recent map.json write.
	assert isinstance(health["last_dump"], (int, float)) and health["last_dump"] > 0


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


# --- resilience: a mapped-but-dead upstream ---------------------------------


def test_dead_upstream_does_not_wedge_proxy():
	# Map a subdomain to an in-subnet address with nothing listening. The proxy
	# must fail that ONE request cleanly (a gateway error, or curl's own timeout if
	# the SYN is dropped) and — the property that matters — keep serving every
	# other route. It must never crash or wedge nginx.
	admin("PUT", "/map/dead", "fd00:a71a:5::dead")
	status, _, _ = fetch("dead", extra=["--max-time", "8"])
	# 502/504 = nginx returned a gateway error; 0 = curl --max-time fired first on a
	# dropped SYN. Both mean "no upstream, no garbage". A 200 would be very wrong.
	assert status in (0, 502, 504), f"dead upstream gave {status}, expected gateway error/timeout"
	# The live route still works right after — one dead upstream didn't wedge nginx.
	admin("PUT", "/map/acme", VM_A)
	assert "upstream=vm-a" in fetch("acme")[1]


# --- TLS floor -------------------------------------------------------------


def test_tls11_refused():
	# nginx.conf pins ssl_protocols TLSv1.2 TLSv1.3. Forcing a 1.1-max handshake
	# must be refused (curl can't negotiate → exits non-zero, status "000"). We
	# cap at 1.1 with --tls-max so curl doesn't fall back up to an allowed version.
	host = f"acme.{REGION}.frappe.dev"
	cmd = [
		"curl",
		"-sk",
		"-o",
		"/dev/null",
		"-w",
		"%{http_code}",
		"--tls-max",
		"1.1",
		"--resolve",
		f"{host}:8443:127.0.0.1",
		f"https://{host}:8443/",
	]
	res = subprocess.run(cmd, capture_output=True, text=True)
	assert res.stdout.strip() in ("", "000"), f"TLS1.1 unexpectedly accepted: {res.stdout!r}"
	assert res.returncode != 0, "curl should fail the handshake"


# --- helpers ---------------------------------------------------------------


def _proxy_master_pid() -> str:
	"""nginx master PID inside the proxy container — to prove no reload."""
	out = subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", "cat", "/run/nginx.pid"],
		cwd=HERE,
		capture_output=True,
		text=True,
		check=True,
	).stdout
	return out.strip()
