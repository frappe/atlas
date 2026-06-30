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
# REGION is the bare region id (scopes the cert dir, certs/<region>/). ZONE is the
# FULL wildcard zone the proxy strips from each Host/SNI — what the region file
# holds (Dockerfile writes ZONE, _finalize_proxy writes active_root_domain().domain).
# ZONE is deliberately DEEPER than "<region>.frappe.dev" (an extra `.x` label) so the
# suffix predicate is exercised against a real platform zone like x.frappe.dev — the
# shape that broke when the lua reconstructed the zone as region .. ".frappe.dev"
# (every wildcard SNI missed → "no host in upstream"). A flat REGION + ".frappe.dev"
# would have hidden that bug, since the two coincide one label under frappe.dev.
REGION = "test"
ZONE = "test.x.frappe.dev"
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
	"""curl the proxy with Host/SNI forced to <subdomain>.<ZONE>.
	Returns (status, body, headers)."""
	host = f"{subdomain}.{ZONE}"
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


def terminator(host: str, path: str = "/", container: str = "proxy") -> tuple[str, str]:
	"""Probe the wildcard TLS terminator DIRECTLY; return (status, body). The terminator
	listens on loopback 127.0.0.1:8443 `ssl proxy_protocol` (the public :443 SNI
	front-door is what's published on host 8443 → container 443), so a host probe can't
	reach it: it's loopback-bound AND requires the PROXY header the front-door emits. We
	curl it FROM INSIDE the container with --haproxy-protocol — exactly the hop the
	front-door makes — so these tests exercise the terminator's own behavior
	(default_server branded 404, host-suffix routing) without depending on the
	front-door's SNI fork. Junk/non-zone SNI at the PUBLIC :443 is a separate concern,
	covered by test_front_door_drops_unroutable_sni below."""
	# Marker must NOT start with '@' — curl's -w reads "@file" as a format file.
	marker = "\nSTATUS::"
	curl = [
		"curl",
		"-sk",
		"--haproxy-protocol",
		"-w",
		marker + "%{http_code}",
		"--resolve",
		f"{host}:8443:127.0.0.1",
		f"https://{host}:8443{path}",
	]
	cmd = ["docker", "compose", "exec", "-T", container, *curl]
	out = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, check=True).stdout
	body, _, status = out.rpartition(marker)
	return status.strip(), body


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
	assert "host=acme.test.x.frappe.dev" in body  # Host preserved end-to-end


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
	# under a configured region → the terminator's branded 404, never a 500. Probed
	# directly at the terminator (the front-door would drop a non-zone SNI at L4 before
	# it ever reaches here — that drop is asserted by test_front_door_drops_unroutable_sni).
	admin("PUT", "/map/acme", VM_A)
	assert terminator("acme.wrongregion.example.com")[0] == "404"


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
	assert "location: https://acme.test.x.frappe.dev" in headers.lower()


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
	host = f"acme.{ZONE}"
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


# ===========================================================================
# Expanded coverage — behaviors, robustness, mistakes (test-expansion plan).
# The originals above prove the happy path; everything below pins the subtler
# behaviors and the failure modes (malformed input, bad upstreams, corrupt
# state) so a regression there can't ship silently.
# ===========================================================================


# --- forwarded headers + request fidelity ----------------------------------


def test_query_string_preserved():
	# proxy_pass $vm_upstream carries no URI part, so nginx forwards $request_uri
	# verbatim — query string + percent-encoding intact, no rewrite. Frappe leans
	# on ?cmd=/?page= everywhere; a proxy_pass form that dropped the query would
	# silently break every parameterized request. (curl strips a real #fragment
	# client-side, so we never assert on one.)
	admin("PUT", "/map/acme", VM_A)
	raw_path = "/app/foo?cmd=bar&page=2&q=a%20b"
	status, body, _ = fetch("acme", path=raw_path)
	assert status == 200
	assert "upstream=vm-a" in body
	assert f"path={raw_path}" in body  # forwarded byte-for-byte


def test_xff_realip_proto_injected():
	# router/nginx.conf inject X-Forwarded-Proto/-For + X-Real-IP on the proxied
	# request. upstream.py echoes them back. A Frappe site behind the proxy relies
	# on X-Forwarded-Proto=https (to know it's TLS) and XFF (for the real client
	# IP); if the proxy stopped sending them, the site would mis-detect http.
	admin("PUT", "/map/acme", VM_A)
	status, body, _ = fetch("acme")
	assert status == 200
	assert "xfproto=https" in body  # the site must see it was reached over TLS
	# XFF / X-Real-IP are present and non-empty (the client/edge IP). We don't pin
	# the value (it's the docker bridge IP) — only that the proxy injected them.
	xff = _echoed(body, "xff")
	xrealip = _echoed(body, "xrealip")
	assert xff, f"X-Forwarded-For not injected: {body!r}"
	assert xrealip, f"X-Real-IP not injected: {body!r}"


def test_connection_header_cleared_non_ws():
	# location / sets proxy_set_header Connection "" — the client's hop-by-hop
	# Connection header must NOT leak to the upstream (it would break upstream
	# keepalive semantics). We send Connection: close; the upstream should echo an
	# empty conn=.
	admin("PUT", "/map/acme", VM_A)
	status, body, _ = fetch("acme", extra=["-H", "Connection: close"])
	assert status == 200
	assert _echoed(body, "conn") == "", f"client Connection leaked to upstream: {body!r}"


def test_host_case_insensitive_routes():
	# Hostnames are case-insensitive. A request whose Host is mixed/upper case must
	# route identically to lowercase. (nginx core lowercases $host before Lua, and
	# router.lua also :lower()s — together they keep routing case-insensitive.)
	admin("PUT", "/map/acme", VM_A)
	# Uppercase the WHOLE host (subdomain + the real zone) — the suffix must still be the
	# active zone or router.lua can't strip it; the point is case, not zone.
	status, body, _ = fetch("acme", extra=["-H", f"Host: {('acme.' + ZONE).upper()}"])
	assert status == 200
	assert "upstream=vm-a" in body
	# Observation (not the assertion's point): the forwarded host is lowercase.
	assert f"host=acme.{ZONE}" in body


def test_sni_host_mismatch_routes_by_host():
	# Routing keys on the Host header, not the TLS SNI. A request with SNI for one
	# subdomain but a Host header for another must route by Host. (Forcing h1.1 +
	# an explicit Host while --resolve sets SNI to acme.)
	admin("PUT", "/map/acme", VM_A)
	admin("PUT", "/map/widgets", VM_B)
	host_sni = f"acme.{ZONE}"  # SNI / cert match
	host_hdr = f"widgets.{ZONE}"  # routing key
	cmd = [
		"curl",
		"-sk",
		"--http1.1",
		"-H",
		f"Host: {host_hdr}",
		"--resolve",
		f"{host_sni}:8443:127.0.0.1",
		f"https://{host_sni}:8443/",
	]
	out = subprocess.run(cmd, capture_output=True, text=True).stdout
	assert "upstream=vm-b" in out, out  # routed by Host, not SNI
	assert f"host={host_hdr}" in out


# --- /socket.io shares the same map ----------------------------------------


def test_socketio_plain_get_routes_and_misses():
	# The /socket.io location runs the SAME router.lua against the SAME dict, so a
	# plain (non-upgrade) GET there proxies on a hit and serves the branded 404 on
	# a miss — the two :443 locations are not independent maps. (test_socketio_upgrade
	# only proves the 101; it never checks /socket.io obeys the map.)
	admin("POST", "/sync", "{}")
	admin("PUT", "/map/acme", VM_A)
	status, body, _ = fetch("acme", path="/socket.io/EIO=4")
	assert status == 200 and "upstream=vm-a" in body
	assert "path=/socket.io/EIO=4" in body
	# A miss on /socket.io still gets the branded page, not a 502/500.
	status, body, _ = fetch("nope", path="/socket.io/")
	assert status == 404 and "isn't here" in body


# --- security headers (values + on the branded page) ------------------------

SEC_HEADERS = {
	"strict-transport-security": "max-age=63072000; includesubdomains; preload",
	"x-frame-options": "sameorigin",
	"x-content-type-options": "nosniff",
	"referrer-policy": "strict-origin-when-cross-origin",
}


def test_security_headers_full_values_on_200():
	# All four security headers land with their exact configured values on a
	# proxied 200. Pins the VALUES (not just presence — test_build covers presence)
	# so a typo'd max-age or a dropped `preload` trips the gate.
	admin("PUT", "/map/acme", VM_A)
	_, _, headers = fetch("acme")
	low = headers.lower()
	for name, value in SEC_HEADERS.items():
		assert f"{name}: {value}" in low, f"{name} value drift: {headers!r}"


def test_security_headers_on_branded_404_and_503():
	# add_header ... always must put the security headers on the Lua-exit branded
	# pages too (404 unknown / 503 tombstone), not only on proxied 200s. Without
	# `always` they'd vanish on non-2xx — a real, subtle regression class.
	admin("POST", "/sync", "{}")
	_, _, h404 = fetch("nope")
	admin("PUT", "/map/paused", "-")
	_, _, h503 = fetch("paused")
	for label, headers in (("404", h404), ("503", h503)):
		low = headers.lower()
		for name in SEC_HEADERS:
			assert f"{name}:" in low, f"{name} missing on branded {label}: {headers!r}"


def test_branded_page_content_type():
	# The branded miss page is served from Lua with an explicit
	# Content-Type: text/html; charset=utf-8 on both the 404 and 503 paths.
	admin("POST", "/sync", "{}")
	for sub, addr, want in (("nope", None, 404), ("paused", "-", 503)):
		if addr:
			admin("PUT", f"/map/{sub}", addr)
		status, _, headers = fetch(sub)
		assert status == want
		assert "content-type: text/html; charset=utf-8" in headers.lower(), headers


def test_branded_page_terminal_no_cycle():
	# router.lua runs ONLY in the two proxy locations, never on the branded page,
	# so a miss renders the page directly with no internal-redirect cycle. Assert
	# nginx never logged a rewrite/redirect cycle while serving misses.
	admin("POST", "/sync", "{}")
	fetch("nope")
	fetch("paused-x", path="/socket.io/")
	res = exec_proxy_text(
		"grep", "-c", "rewrite or internal redirection cycle", "/var/log/nginx/error.log", check=False
	)
	count = res.stdout.strip() or "0"
	assert count == "0", f"redirect cycle in error.log: {count}"


# --- :80 ACME passthrough + default_server ----------------------------------


def test_acme_challenge_served_not_redirected():
	# The :80 server passes /.well-known/acme-challenge/ through to a file root
	# (HTTP-01 support), instead of 308-redirecting it to https like everything
	# else. Drop a token and assert it's served plain, no Location.
	token_dir = "/var/lib/nginx/acme/.well-known/acme-challenge"
	exec_proxy_text("mkdir", "-p", token_dir)
	exec_proxy_text("sh", "-c", f"printf 'TOKEN-OK' > {token_dir}/probe")
	host = f"acme.{ZONE}"
	cmd = [
		"curl",
		"-s",
		"-D",
		"/dev/stderr",
		"--resolve",
		f"{host}:8080:127.0.0.1",
		f"http://{host}:8080/.well-known/acme-challenge/probe",
	]
	res = subprocess.run(cmd, capture_output=True, text=True)
	assert res.stdout == "TOKEN-OK", f"acme token not served: {res.stdout!r}"
	assert "location:" not in res.stderr.lower(), f"acme challenge was redirected: {res.stderr!r}"
	# Contrast: a normal path on :80 still redirects.
	status, _, _ = fetch("acme", scheme="http")
	assert status == 308


def test_default_server_handles_bare_ip_host():
	# A request whose Host has no derivable subdomain under the region (a bare IP, or
	# the bare zone with no subdomain label) reaches the terminator's default_server and
	# gets the branded 404 — never a 500/000. Probed directly at the terminator; such a
	# name carries no zone-matching SNI, so at the public :443 it is dropped at L4
	# (test_front_door_drops_unroutable_sni), never reaching the default_server.
	admin("PUT", "/map/acme", VM_A)  # seed something routable
	for host in ("127.0.0.1", ZONE):
		assert terminator(host)[0] == "404", f"bare-IP host {host!r} did not get branded 404"


def test_front_door_drops_unroutable_sni():
	# The public :443 SNI front-door (sni_router.lua) routes raw TLS by the ClientHello
	# SNI: a name under the wildcard zone → the terminator (8443), a known custom domain
	# → the strip-path (8445), anything else → DROP at L4 (ngx.exit(ngx.ERROR)). So an
	# SNI that is neither — a foreign domain, a bare IP with no SNI, or the bare zone with
	# no subdomain label — gets no handshake at all: curl fails (non-zero rc / 000), not a
	# branded 404. The 404 is the terminator's answer for a name that DID reach it
	# (test_default_server_handles_bare_ip_host, via --haproxy-protocol); this is the
	# layer below — junk never gets past the front-door to be 404'd.
	admin("PUT", "/map/acme", VM_A)  # a populated map must not matter
	for host in ("acme.wrongregion.example.com", "127.0.0.1", ZONE):
		cmd = [
			"curl",
			"-sk",
			"-o",
			"/dev/null",
			"-w",
			"%{http_code}",
			"--resolve",
			f"{host}:{HTTPS.rpartition(':')[2]}:127.0.0.1",
			f"https://{host}:{HTTPS.rpartition(':')[2]}/",
		]
		res = subprocess.run(cmd, capture_output=True, text=True)
		status = res.stdout.strip()
		# L4 drop: no TLS handshake completes → curl exits non-zero and reports 000.
		assert res.returncode != 0, f"{host!r} unexpectedly completed (rc=0, status={status})"
		assert status in ("000", ""), f"{host!r} got HTTP {status}, expected an L4 drop"


# --- admin route/method dispatch -------------------------------------------


def test_get_map_sub_404_shape():
	# GET /map/<unknown> returns a distinct {"error":"no such subdomain"} — NOT the
	# same body as an unknown admin route. The controller distinguishes them.
	admin("POST", "/sync", "{}")
	status, body = admin("GET", "/map/ghost")
	assert status == 404 and json.loads(body)["error"] == "no such subdomain"
	status, body = admin("GET", "/nope")
	assert status == 404 and "unknown route" in body.lower()


def test_method_dispatch_405_vs_404():
	# A known /map/<sub> with an unhandled method → 405; an unknown route → 404.
	# admin.lua dispatches the per-sub verbs (GET/PUT/DELETE) and 405s the rest.
	for method in ("POST", "PATCH"):
		status, body = admin(method, "/map/acme")
		assert status == 405, f"{method} /map/acme gave {status}"
		assert "method not allowed" in body.lower(), body
	# PUT /map (no sub) isn't a per-sub route → unknown route 404.
	status, body = admin("PUT", "/map")
	assert status == 404 and "unknown route" in body.lower()


def test_admin_wrong_method_route_combos():
	# A sweep of unhandled (method, route) combos must each return a clean 4xx and
	# never raise (i.e. never crash the admin worker / never CalledProcessError).
	combos = [
		("DELETE", "/healthz"),
		("PUT", "/sync"),
		("DELETE", "/map"),
		("POST", "/map/acme"),
		("GET", "/dump"),
	]
	for method, route in combos:
		status, _ = admin(method, route)
		assert status in (404, 405), f"{method} {route} gave {status}, want 404/405"


# --- PUT address hygiene ---------------------------------------------------


def test_put_trailing_whitespace_stripped():
	# admin.lua strips trailing whitespace from a PUT address (the body often
	# arrives with a trailing newline over SSH stdin). The stored + echoed address
	# must be the clean literal and the route must work.
	admin("PUT", "/map/ws", VM_A + "\n  ")
	status, body = admin("GET", "/map/ws")
	assert status == 200 and json.loads(body)["address"] == VM_A
	assert "upstream=vm-a" in fetch("ws")[1]


def test_put_only_whitespace_rejected():
	# A PUT whose body is only whitespace strips to "" → 400 empty, and stores
	# nothing (a blank mapping would route to http://[]:80).
	status, body = admin("PUT", "/map/blank2", "   \n\t")
	assert status == 400 and "empty" in body.lower()
	assert admin("GET", "/map/blank2")[0] == 404  # nothing was stored


# --- bad addresses fail clean, never 200 ------------------------------------


def test_empty_address_routes_clean_not_200():
	# An empty-string address passes /sync's type validation (it IS a string) but
	# is nonsense — router builds http://[]:80. The request must fail clean (5xx /
	# curl error), never 200/garbage, and must not wedge the proxy.
	admin("POST", "/sync", json.dumps({"empty": ""}))
	status, body, _ = fetch("empty", extra=["--max-time", "8"])
	assert status == 0 or status >= 500, f"empty addr gave {status}"
	assert "upstream=" not in body
	# Live routes + admin still fine afterwards.
	admin("PUT", "/map/acme", VM_A)
	assert "upstream=vm-a" in fetch("acme")[1]
	assert admin("GET", "/healthz")[0] == 200


def test_non_v6_address_fails_clean():
	# A mapped address that isn't a bare v6 literal (a v4 literal, garbage, or an
	# already-bracketed addr → double bracket) makes a malformed upstream URL.
	# nginx must reject it cleanly (5xx) and never 200. router.lua blindly wraps
	# the address in http://[...]:80, so this is the operator-mistake guard.
	for bad in ("1.2.3.4", "garbage", f"[{VM_A}]"):
		admin("PUT", "/map/badaddr", bad)
		status, body, _ = fetch("badaddr", extra=["--max-time", "8"])
		assert status == 0 or status >= 500, f"addr {bad!r} gave {status}"
		assert "upstream=" not in body, f"addr {bad!r} reached an upstream"
	# Proxy still healthy + a good route works.
	admin("PUT", "/map/acme", VM_A)
	assert "upstream=vm-a" in fetch("acme")[1]


def test_misbehaving_upstream_502_not_crash():
	# A mapped-but-broken upstream (non-HTTP garbage / truncated body) must make
	# the proxy fail the ONE request cleanly — a gateway error or a curl transport
	# failure — never crash, wedge, or pass garbage through as a 200. vm-bad picks
	# its failure mode from the forwarded Host (see misbehave.py).
	admin("PUT", "/map/garbage", VM_BAD)  # Host "garbage..." → non-HTTP reply
	status, body, _ = fetch("garbage", extra=["--max-time", "8"])
	assert status == 0 or status >= 500, f"garbage upstream gave {status}"
	assert "upstream=" not in body
	# Truncated mode: a valid 200 status line but a short body → the client read
	# fails (curl exits non-zero) even if a status slips through.
	admin("PUT", "/map/truncated", VM_BAD)  # Host "truncated..." → short body
	rc = fetch_rc("truncated", extra=["--max-time", "8"])
	assert rc != 0, "truncated upstream should fail the client transfer"
	# One bad upstream didn't wedge the proxy.
	admin("PUT", "/map/acme", VM_A)
	assert "upstream=vm-a" in fetch("acme")[1]


# --- weird Host / weird subdomain keys -------------------------------------


def test_weird_host_headers_degrade():
	# Odd Host headers must degrade SAFELY — a clean 4xx, never a 500 and never a
	# wrong route. We keep SNI valid (--resolve on a real wildcard host) and
	# override only the Host header so the request reaches the proxy:
	#   - a leading-dot host strips to an EMPTY subdomain → router's branded 404
	#   - a v4 literal has no ".<region>.frappe.dev" suffix → router's branded 404
	#   - a raw (unbracketed) v6 literal is an invalid Host per nginx's own parser,
	#     so NGINX rejects it with 400 before Lua even runs — also a clean degrade.
	# (Sending the weird value via --resolve instead would make curl reject the
	# resolve entry before the proxy ever sees it — a curl artifact, not behavior.)
	admin("PUT", "/map/acme", VM_A)
	sni = f"acme.{ZONE}"
	expect = {
		f".{ZONE}": ("404",),  # empty subdomain → branded 404
		"192.0.2.7": ("404",),  # no region suffix → branded 404
		"fd00:a71a:5::1": ("400",),  # invalid Host → nginx 400 (pre-Lua)
	}
	for host, want in expect.items():
		cmd = [
			"curl",
			"-sk",
			"-o",
			"/dev/null",
			"-w",
			"%{http_code}",
			"-H",
			f"Host: {host}",
			"--resolve",
			f"{sni}:8443:127.0.0.1",
			f"https://{sni}:8443/",
		]
		status = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
		assert status in want, f"host {host!r} gave {status}, want one of {want}"
	assert admin("GET", "/healthz")[0] == 200  # still healthy


def test_weird_subdomain_keys_literal():
	# A subdomain key with unusual chars is stored + retrieved verbatim through the
	# admin CRUD and round-trips through the canonical JSON serializer (so a future
	# key with a quote/backslash can't corrupt map.json). URL-encode the key in the
	# admin path; assert the stored value is byte-identical.
	admin("POST", "/sync", "{}")
	weird = {
		"sub.with.dots": VM_A,
		"quote%22key": VM_A,  # %22 = a literal " in the key
		"back%5Cslash": VM_B,  # %5C = a literal backslash
	}
	for key, addr in weird.items():
		status, _ = admin("PUT", f"/map/{key}", addr)
		assert status == 200, key
	# GET /map must be valid JSON with the decoded literal keys present.
	_, body = admin("GET", "/map")
	live = json.loads(body)
	assert live.get("sub.with.dots") == VM_A
	assert live.get('quote"key') == VM_A
	assert live.get("back\\slash") == VM_B
	admin("POST", "/sync", "{}")  # reset


def test_sync_duplicate_keys_last_wins():
	# A /sync body with a duplicate JSON key is decoded by cjson last-wins; the map
	# ends with one entry at the final value. A buggy decoder that kept both / the
	# first would route to the wrong VM.
	body = '{"dup": "%s", "dup": "%s"}' % (VM_A, VM_B)
	status, _ = admin("POST", "/sync", body)
	assert status == 200
	_, got = admin("GET", "/map/dup")
	assert json.loads(got)["address"] == VM_B  # last value won
	_, full = admin("GET", "/map")
	assert list(json.loads(full).keys()).count("dup") == 1
	admin("POST", "/sync", "{}")


# --- oversized admin body --------------------------------------------------


def test_large_sync_body_spills_and_applies():
	# A big /sync body (read_body spills to a temp file past the in-memory limit)
	# must apply fully. 8000 entries proves the spill path works end to end.
	desired = {f"s{i}": (VM_A if i % 2 else VM_B) for i in range(8000)}
	status, _ = admin("POST", "/sync", json.dumps(desired))
	assert status == 200
	_, health = admin("GET", "/healthz")
	assert json.loads(health)["entries"] == 8000
	assert "upstream=vm-a" in fetch("s4243")[1]  # an odd-index entry → vm-a
	admin("POST", "/sync", "{}")


# --- concurrency: atomic /sync, coherent CRUD -------------------------------


def test_concurrent_reads_during_sync_never_partial():
	# A reader hammering GET /map while a /sync runs must always see a COMPLETE map
	# (old-complete or new-complete), never a half-applied one — admin.lua upserts
	# desired then deletes the leftovers (no flush_all window). We flip between two
	# disjoint 200-entry maps under a concurrent reader and assert the reader never
	# observes a count outside {old, new} (a partial write would show an in-between
	# count).
	map_a = {f"a{i}": VM_A for i in range(200)}
	map_b = {f"b{i}": VM_B for i in range(200)}
	admin("POST", "/sync", json.dumps(map_a))
	stop = threading.Event()
	seen_bad = []

	def reader():
		while not stop.is_set():
			_, body = admin("GET", "/map")
			n = len(json.loads(body))
			# Either fully map_a (200 a-keys) or fully map_b (200 b-keys). A count
			# that isn't 200 means a torn read. (Equal sizes, disjoint keys.)
			if n != 200:
				seen_bad.append(n)

	t = threading.Thread(target=reader, daemon=True)
	t.start()
	try:
		for i in range(6):
			admin("POST", "/sync", json.dumps(map_b if i % 2 else map_a))
	finally:
		stop.set()
		t.join(timeout=5)
	assert not seen_bad, f"reader saw partial map sizes: {seen_bad[:10]}"
	admin("POST", "/sync", "{}")


def test_concurrent_crud_stays_coherent():
	# Concurrent admin writes (a /sync plus per-sub PUT/DELETE) must leave the map
	# coherent: GET /map is valid canonical JSON, every value a string, the proxy
	# still serves, and GET /map equals the on-disk dump after a forced /dump.
	admin("POST", "/sync", "{}")
	ops = [
		lambda: admin("POST", "/sync", json.dumps({"a": VM_A, "b": VM_B, "c": VM_A})),
		lambda: admin("PUT", "/map/d", VM_B),
		lambda: admin("DELETE", "/map/a"),
		lambda: admin("PUT", "/map/e", VM_A),
	]
	threads = [threading.Thread(target=op) for op in ops]
	for t in threads:
		t.start()
	for t in threads:
		t.join(timeout=5)
	# Coherent: valid JSON, all string values, healthy, keys ⊆ the op set.
	_, body = admin("GET", "/map")
	live = json.loads(body)
	assert all(isinstance(v, str) for v in live.values())
	assert set(live).issubset({"a", "b", "c", "d", "e"})
	assert admin("GET", "/healthz")[0] == 200
	# GET /map is byte-identical to the dumped file (canonical serializer agrees).
	admin("POST", "/dump")
	_, served = admin("GET", "/map")
	on_disk = exec_proxy_text("cat", "/var/lib/nginx/map.json").stdout
	assert served == on_disk, "GET /map != on-disk map.json after dump"
	admin("POST", "/sync", "{}")


# --- persistence timing: debounce + durability window -----------------------


def test_debounce_coalesces_burst():
	# persist.schedule_dump() debounces 1s, so a burst of writes coalesces into
	# FEWER dumps than writes (ideally one). We quiesce, then fire 8 quick PUTs and
	# assert the number of distinct last_dump epochs observed is < 8 (coalesced)
	# and at least one dump eventually lands. (Direction, not "exactly one" — that
	# flakes on a slow box.)
	admin("POST", "/sync", "{}")
	admin("POST", "/dump")  # land a baseline dump
	time.sleep(1.2)  # let any pending debounce timer fire
	baseline = _last_dump()
	for i in range(8):
		admin("PUT", f"/map/burst{i}", VM_A)
	# Sample last_dump across the debounce settle; collect the distinct values.
	seen = set()
	deadline = time.time() + 4
	while time.time() < deadline:
		ld = _last_dump()
		if ld:
			seen.add(round(ld, 3))
		time.sleep(0.1)
	after = _last_dump()
	assert after and after > baseline, f"burst never dumped (baseline={baseline}, after={after})"
	# Distinct dump timestamps during/after the burst must be far fewer than 8.
	assert len(seen) < 8, f"writes did not coalesce: {len(seen)} distinct dumps for 8 writes"
	admin("POST", "/sync", "{}")


def test_undumped_write_lost_on_restart():
	# The debounce is a durability WINDOW: a write that hasn't been dumped yet is
	# lost if the proxy restarts before the 1s timer fires (the on-disk map.json is
	# the only thing reloaded). Atlas's reconcile is the backstop, so this is
	# INTENDED — we pin it so the window is understood, not silently widened.
	# Control: a dumped write survives. Subject: an immediately-restarted unforced
	# write does not.
	admin("POST", "/sync", "{}")
	admin("PUT", "/map/durable", VM_A)
	admin("POST", "/dump")  # force it to disk
	# Now an unforced write, then restart FAST (before the ~1s debounce dump).
	admin("PUT", "/map/ephemeral", VM_A)
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	_wait_for_socket()
	assert "upstream=vm-a" in fetch("durable")[1], "dumped write did not survive restart"
	assert fetch("ephemeral")[0] == 404, "un-dumped write unexpectedly survived (debounce window widened?)"
	admin("POST", "/sync", "{}")
	admin("POST", "/dump")


# --- corrupt on-disk state boots clean --------------------------------------


@pytest.mark.parametrize("corrupt", ["{garbage", "42", "[1,2,3]", '{"acme":', '{"acme": 5}'])
def test_corrupt_mapjson_boots_and_serves(corrupt):
	# A torn / wrong-typed map.json must not crash-loop the proxy at boot:
	# persist.load ignores a non-object and skips bad entries; the dict comes up
	# empty (or partial) and the proxy serves the branded 404 rather than failing
	# to start. _wait_for_socket() is the crash-loop oracle.
	exec_proxy_text("sh", "-c", f"printf '%s' {json.dumps(corrupt)} > /var/lib/nginx/map.json")
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	_wait_for_socket()  # raises if the proxy never came back up
	assert admin("GET", "/healthz")[0] == 200
	assert fetch("nope")[0] == 404  # serves cleanly, no crash
	# A map.json with a string key but a NON-string value ({"acme": 5}) — if the
	# loader stored it, router would build a bogus upstream; assert it fails clean.
	if corrupt == '{"acme": 5}':
		status, body, _ = fetch("acme", extra=["--max-time", "8"])
		assert status == 0 or status >= 500
		assert "upstream=" not in body
	# Reset to a known-clean snapshot for the next test.
	admin("POST", "/sync", "{}")
	admin("POST", "/dump")


# --- empty-region first-label fallback (proxy-noregion service) -------------


def test_empty_region_first_label_fallback():
	# With NO region configured, router.lua (the L7 terminator) falls back to routing by
	# the first host label (everything before the first dot) instead of 500ing. The
	# proxy-noregion service runs the SAME image with an empty /var/lib/nginx/region. This
	# is a terminator behavior, so we probe its terminator directly (the empty-region
	# front-door has no zone to match an SNI against, so it can't fork there): a dotted
	# host routes by its first label; a dotless host has no label to strip → branded 404.
	noregion_admin("PUT", "/map/acme", VM_A)
	status, body = terminator("acme.anything.example.com", container="proxy-noregion")
	assert status == "200", (status, body)
	assert "upstream=vm-a" in body, body
	# A dotless host (no first label to strip) → branded 404, not a 500.
	assert terminator("acme", container="proxy-noregion")[0] == "404"


# --- helpers ---------------------------------------------------------------


VM_BAD = "fd00:a71a:5::bad"


def fetch_rc(subdomain: str, path: str = "/", extra: list[str] | None = None) -> int:
	"""Like fetch() but returns curl's exit code — for asserting a transport
	failure (e.g. an upstream that closes mid-body) rather than a status."""
	host = f"{subdomain}.{ZONE}"
	cmd = [
		"curl",
		"-sk",
		"-o",
		"/dev/null",
		"--resolve",
		f"{host}:8443:127.0.0.1",
		f"https://{host}:8443{path}",
	]
	if extra:
		cmd += extra
	return subprocess.run(cmd, capture_output=True, text=True).returncode


def _echoed(body: str, key: str) -> str:
	"""Pull a `key=value` token out of upstream.py's echo line (value runs to the
	next space or EOL). Returns '' if absent/empty."""
	for tok in body.split():
		if tok.startswith(key + "="):
			return tok[len(key) + 1 :]
	return ""


def _last_dump() -> float | None:
	"""GET /healthz → last_dump epoch (or None if never dumped)."""
	_, body = admin("GET", "/healthz")
	return json.loads(body).get("last_dump")


def noregion_admin(method: str, path: str, body: str | None = None) -> tuple[int, str]:
	"""admin() against the proxy-noregion container's socket (empty-region proxy)."""
	curl = ["curl", "-s", "-o", "-", "-w", "\n%{http_code}", "--unix-socket", ADMIN_SOCK, "-X", method]
	if body is not None:
		curl += ["--data-binary", "@-"]
	curl.append(f"http://localhost{path}")
	cmd = ["docker", "compose", "exec", "-T", "proxy-noregion", *curl]
	out = subprocess.run(cmd, cwd=HERE, input=body, capture_output=True, text=True, check=True).stdout
	payload, _, status = out.rpartition("\n")
	return int(status), payload


def exec_proxy_text(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
	"""Run a command inside the proxy container (for inspecting/seeding state)."""
	return subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", *argv],
		cwd=HERE,
		capture_output=True,
		text=True,
		check=check,
	)


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
