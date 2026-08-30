#!/usr/bin/env python3
# Proxy latency / timing / scale gate (test-expansion plan §3C). The companion
# test_proxy.py proves the proxy BEHAVES correctly; this file proves it behaves
# correctly UNDER LOAD and within sane TIMING bounds — the "latencies? delays?"
# half of the ask.
#
# Philosophy: these are REGRESSION guards, not benchmarks. Every test PRINTS the
# observed medians/p95 (so CI shows the real numbers) but asserts only GENEROUS
# ceilings — tight sub-100ms claims flake on macOS Docker, so we assert direction
# and order-of-magnitude, not precise latency. A failure here means something got
# dramatically slower (a reload crept in, buffering turned on, the dict went
# linear), not that a request was 5ms over budget.
#
# Run the stack first:  docker compose up --build -d
# Then:                 python3 -m pytest test_latency.py -v
#
# Reuses test_proxy.py's transport (admin()/fetch()) so the harness is identical.

import json
import os
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from test_proxy import (
	HERE,
	REGION,
	VM_A,
	admin,
	exec_proxy_text,
	fetch,
)


def _pct(values: list[float], p: float) -> float:
	"""Crude percentile (nearest-rank) — good enough for a regression ceiling."""
	if not values:
		return 0.0
	s = sorted(values)
	k = max(0, min(len(s) - 1, round(p / 100.0 * (len(s) - 1))))
	return s[k]


def _report(name: str, values: list[float]) -> None:
	if not values:
		print(f"\n[{name}] no samples")
		return
	print(
		f"\n[{name}] n={len(values)} "
		f"min={min(values) * 1000:.1f}ms "
		f"median={statistics.median(values) * 1000:.1f}ms "
		f"p95={_pct(values, 95) * 1000:.1f}ms "
		f"max={max(values) * 1000:.1f}ms"
	)


# --- per-request routing overhead ------------------------------------------


def test_routing_overhead_bounded():
	# The Lua access phase is "one dict read, no allocation" (router.lua). Compare
	# the median proxied request latency against a DIRECT hit to the same upstream
	# (from inside the proxy container, no TLS). The proxy adds TLS + a dict lookup;
	# the overhead must stay small — a blow-up would mean a reload-per-request or a
	# linear dict scan crept in. Generous ceilings: median < 250ms AND < 25x direct.
	admin("PUT", "/map/acme", VM_A)
	# Warm up (TLS session, upstream connect).
	for _ in range(5):
		fetch("acme")
	proxied = []
	for _ in range(60):
		t = time.time()
		status, body, _ = fetch("acme")
		proxied.append(time.time() - t)
		assert status == 200 and "upstream=vm-a" in body
	direct = _direct_upstream_times(60)
	_report("proxied", proxied)
	_report("direct", direct)
	med_proxied = statistics.median(proxied)
	med_direct = statistics.median(direct) or 0.001
	assert med_proxied < 0.25, f"proxied median {med_proxied * 1000:.0f}ms too high"
	# Order-of-magnitude guard — a curl-per-call has fixed cost, so the ratio is
	# loose. A 25x blow-up means real overhead (reload, scan), not measurement.
	assert med_proxied < med_direct * 25 + 0.1, (
		f"proxy overhead {med_proxied / med_direct:.1f}x direct (median "
		f"{med_proxied * 1000:.0f}ms vs {med_direct * 1000:.0f}ms)"
	)


# --- streaming: first byte before the full body ----------------------------


def test_streaming_first_byte_before_body():
	# location / sets proxy_buffering off for Frappe streaming. The /__stream
	# upstream flushes "A", sleeps 2s, then "B". A streaming proxy delivers the
	# first byte (time_starttransfer) well before the body completes
	# (time_total ≈ 2s); a BUFFERING proxy would withhold everything until the
	# upstream finished, making starttransfer ≈ total. Assert the gap is real.
	admin("PUT", "/map/acme", VM_A)
	host = f"acme.{REGION}.frappe.dev"
	# NB: a curl -w string must NOT start with '@' (curl reads @file then). Use a
	# leading marker word and split it back off. Body goes to stdout, timing too —
	# the marker keeps them separable regardless of body content.
	marker = "TIMING:"
	cmd = [
		"curl",
		"-sk",
		"--max-time",
		"10",
		"-w",
		"\n" + marker + "%{time_starttransfer} %{time_total}",
		"--resolve",
		f"{host}:8443:127.0.0.1",
		f"https://{host}:8443/__stream",
	]
	res = subprocess.run(cmd, capture_output=True, text=True)
	body, _, timing = res.stdout.rpartition(marker)
	starttransfer, total = (float(x) for x in timing.split())
	print(f"\n[stream] starttransfer={starttransfer * 1000:.0f}ms total={total * 1000:.0f}ms body={body!r}")
	assert "A" in body and "B" in body, f"stream body incomplete: {body!r}"
	assert total >= 1.8, f"total {total:.2f}s — upstream sleep(2s) not observed"
	# First byte must arrive well before the body completes — the streaming proof.
	assert (total - starttransfer) >= 1.5, (
		f"first byte at {starttransfer:.2f}s, total {total:.2f}s — proxy appears to BUFFER"
	)


# --- TLS session resumption is cheaper -------------------------------------


def test_tls_session_resumption_works():
	# ssl_session_cache shared:MozSSL + a TLS1.2 handshake → a second connection
	# can RESUME (skip the full handshake). openssl s_client -sess_out/-sess_in
	# across two invocations: the first is "New", the second "Reused". Proves the
	# session cache is live (a resumed handshake is materially cheaper).
	sess = "/tmp/proxy_sess.pem"
	host = f"acme.{REGION}.frappe.dev"
	first = _openssl_session(host, sess_out=sess)
	assert "New, " in first or "Session-ID:" in first, f"first handshake odd:\n{first[-400:]}"
	second = _openssl_session(host, sess_in=sess)
	# Reuse shows up as "Reused" in the s_client summary.
	assert "Reused" in second, f"TLS1.2 session was NOT resumed:\n{second[-400:]}"


# --- concurrency soak: zero errors, no reload ------------------------------


def test_concurrency_soak_zero_errors():
	# worker_connections 16384 — the proxy must serve a burst of concurrent clients
	# with zero errors and without reloading nginx. 20 workers x 100 requests = 2000
	# routed requests; every one must be a correct 200 to the right upstream, the
	# master PID must be unchanged after, and /healthz still green.
	from test_proxy import _proxy_master_pid

	admin("PUT", "/map/load", VM_A)
	pid_before = _proxy_master_pid()
	latencies = []
	errors = []

	def one(_i: int) -> None:
		t = time.time()
		status, body, _ = fetch("load")
		latencies.append(time.time() - t)
		if status != 200 or "upstream=vm-a" not in body:
			errors.append((status, body[:80]))

	with ThreadPoolExecutor(max_workers=20) as pool:
		list(pool.map(one, range(2000)))
	_report("soak", latencies)
	assert not errors, f"{len(errors)} soak errors, first few: {errors[:5]}"
	assert _proxy_master_pid() == pid_before, "nginx reloaded under load"
	assert admin("GET", "/healthz")[0] == 200
	admin("POST", "/sync", "{}")


# --- large map: scale of /sync + lookup ------------------------------------


def test_large_map_syncs_and_routes():
	# The dict claims ~250k+ entries in 64m. Push 10k entries via one /sync and
	# assert it applies within a sane time, GET /map is sorted+complete, and a
	# lookup at the start/middle/end of the keyspace still routes correctly (the
	# dict is hashed, so lookup is O(1) regardless of size).
	desired = {f"site{i:05d}": (VM_A if i % 2 else "fd00:a71a:5::b") for i in range(10000)}
	t = time.time()
	status, _ = admin("POST", "/sync", json.dumps(desired))
	elapsed = time.time() - t
	print(f"\n[large-map] /sync of 10000 entries took {elapsed:.2f}s")
	assert status == 200
	assert elapsed < 30, f"/sync of 10k entries took {elapsed:.1f}s — too slow"
	_, health = admin("GET", "/healthz")
	assert json.loads(health)["entries"] == 10000
	# A routed lookup at three points in the (sorted) keyspace — all O(1).
	for i in (1, 5000, 9999):
		sub = f"site{i:05d}"
		want = "vm-a" if i % 2 else "vm-b"
		assert f"upstream={want}" in fetch(sub)[1], f"{sub} routed wrong"
	admin("POST", "/sync", "{}")


# --- cold start: route ready when healthz reports it -----------------------


def test_cold_start_route_ready_with_healthz():
	# After a restart, the dict repopulates from map.json at worker init. There must
	# be NO window where /healthz reports 200+entries but a routed request still
	# 404s (init_worker loads before serving). Seed+dump, restart, then the FIRST
	# healthz that reports the entry must coincide with the route already working.
	admin("POST", "/sync", "{}")
	admin("PUT", "/map/acme", VM_A)
	admin("POST", "/dump")
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	# Poll healthz until it reports the entry; then the route must already serve.
	deadline = time.time() + 30
	ready_at = None
	while time.time() < deadline:
		try:
			status, body = admin("GET", "/healthz")
		except subprocess.CalledProcessError:
			time.sleep(0.05)
			continue
		if status == 200 and json.loads(body).get("entries", 0) >= 1:
			ready_at = time.time()
			break
		time.sleep(0.05)
	assert ready_at, "healthz never reported the restored entry within 30s"
	# The very next routed request must already work — no healthz-green-but-404 gap.
	status, body, _ = fetch("acme")
	assert status == 200 and "upstream=vm-a" in body, (
		f"route not ready when healthz said entries>=1: {status} {body[:80]!r}"
	)


# --- helpers ---------------------------------------------------------------


def _direct_upstream_times(n: int) -> list[float]:
	"""Time n direct HTTP hits to vm-a from inside the proxy container (no TLS, no
	proxy) — the baseline the proxied latency is measured against."""
	times = []
	for _ in range(n):
		res = exec_proxy_text(
			"curl",
			"-s",
			"-o",
			"/dev/null",
			"-w",
			"%{time_total}",
			"http://[fd00:a71a:5::a]:80/",
		)
		try:
			times.append(float(res.stdout.strip()))
		except ValueError:
			pass
	return times


def _openssl_session(host: str, sess_out: str | None = None, sess_in: str | None = None) -> str:
	"""Run openssl s_client INSIDE the proxy container against :443 (TLS1.2 so the
	session-ID resumption path is exercised; TLS1.3 uses tickets which s_client
	reports differently). Returns the s_client summary text. -sess_out writes the
	session for a later -sess_in to resume."""
	args = [
		"openssl",
		"s_client",
		"-connect",
		"127.0.0.1:443",
		"-servername",
		host,
		"-tls1_2",
	]
	if sess_out:
		args += ["-sess_out", sess_out]
	if sess_in:
		args += ["-sess_in", sess_in]
	# s_client reads stdin for the HTTP request; send a tiny GET then close so it
	# completes the handshake and exits.
	res = subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", *args],
		cwd=HERE,
		input="GET / HTTP/1.0\r\n\r\n",
		capture_output=True,
		text=True,
	)
	return res.stdout + res.stderr
