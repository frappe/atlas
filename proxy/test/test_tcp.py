#!/usr/bin/env python3
# TCP proxy image-level release gate (spec/17-tcp-proxy.md). Drives the running
# docker-compose stack's L4 forwarder: SYNC a port->backend map through the
# stream-admin line-protocol socket, connect to a published proxy port, assert the
# bytes round-trip to the raw upstream, then remap and assert NO reload — the L4
# mirror of test_proxy.py's HTTP gate.
#
# Run the stack first:  docker compose up --build -d
# Then:                 python3 -m pytest test_tcp.py -v
# Teardown:             docker compose down -v
#
# The stream admin is NOT HTTP (a 4-verb line protocol: GET/STAT/SYNC/DUMP), so we
# drive it via the `stream-admin` client baked into the test image (the L4 analogue
# of the HTTP test's `curl --unix-socket`), exec'd inside the proxy container —
# faithful to production, where Atlas reaches the socket over SSH-to-the-guest,
# never a mount.
#
# The first block (forward / remap / unmapped / sync / canonical-GET / restart) is
# the happy-path gate. Everything below the "Expanded coverage" banner pins the
# subtler L4 behaviors and failure modes — malformed-body rejection that leaves the
# map intact, a dead/misbehaving backend that must not wedge the forwarder,
# concurrent-SYNC coherence, the debounce durability window, corrupt-on-disk boot,
# the STAT observability verb — bringing the TCP gate up to the HTTP gate's depth
# (test_proxy.py). Many HTTP tests have NO L4 analogue (no Host, headers, TLS, or
# branded page exist for raw TCP); only the L4-meaningful ones are mirrored here.

import concurrent.futures
import json
import os
import socket
import subprocess
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

# Published proxy ports (out of the pre-opened 10000-19999 pool) → localhost.
PORT_A = 10000
PORT_B = 10001
PORT_C = 10002  # the "spare" port: unmapped→mapped + dead/misbehaving-backend cases

# The raw-TCP upstreams' v6 literals + service port (see docker-compose.yml).
TCP_A = "[fd00:a71a:5::c]:7000"
TCP_B = "[fd00:a71a:5::d]:7000"

# Misbehaving raw-TCP backends (tcp_misbehave.py, one per failure mode).
TCP_SILENT = "[fd00:a71a:5::e]:7000"  # accept, then never send / never close
TCP_RST = "[fd00:a71a:5::f]:7000"  # accept, then immediately close, no data
# A v6 literal in-subnet with nothing listening — the SYN is dropped/refused.
TCP_DEAD = "[fd00:a71a:5::dead]:7000"


def stream_admin(verb: str, body: str | None = None) -> str:
	"""Speak the stream-admin line protocol FROM INSIDE the proxy container (Atlas
	reaches it over SSH-to-the-guest in production). Returns the reply text."""
	cmd = ["docker", "compose", "exec", "-T", "proxy", "stream-admin", verb]
	return subprocess.run(cmd, cwd=HERE, input=body, capture_output=True, text=True, check=True).stdout


def sync(port_map: dict[str, str]) -> None:
	"""Bulk-replace the served map with the canonical JSON of `port_map` (the same
	sorted/indented bytes Atlas's canonical_json emits)."""
	body = json.dumps(port_map, sort_keys=True, indent=2) + "\n"
	stream_admin("SYNC", body)


def proxy_connect(port: int, send: bytes = b"", timeout: float = 5.0) -> bytes:
	"""Open a TCP connection to a published proxy port on localhost, optionally
	send `send`, and read what comes back (banner + any echo). Returns the bytes."""
	with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
		sock.settimeout(timeout)
		if send:
			sock.sendall(send)
		chunks = []
		try:
			while True:
				data = sock.recv(4096)
				if not data:
					break
				chunks.append(data)
				# The upstream banners then echoes; once we've seen the banner and
				# (if we sent) the echo, stop so the test doesn't hang on the open
				# socket. A short follow-up read with a tight deadline drains the rest.
				if b"\n" in b"".join(chunks) and (not send or send in b"".join(chunks)):
					break
		except TimeoutError:
			pass
		return b"".join(chunks)


@pytest.fixture(scope="module", autouse=True)
def clean_map():
	"""Each module run starts from a known map and ends clean."""
	_wait_for_stream_admin()
	sync({str(PORT_A): TCP_A, str(PORT_B): TCP_B})
	yield
	sync({})


def _wait_for_stream_admin(timeout: float = 30.0) -> None:
	deadline = time.time() + timeout
	last = ""
	while time.time() < deadline:
		try:
			last = stream_admin("GET")
			# A valid empty-or-populated map is JSON; that means the socket is up.
			json.loads(last)
			return
		except (subprocess.CalledProcessError, json.JSONDecodeError):
			pass
		time.sleep(0.5)
	raise RuntimeError(f"stream-admin socket never came up (last reply: {last!r})")


# --- forward end-to-end ----------------------------------------------------


def test_forward_reaches_mapped_backend():
	sync({str(PORT_A): TCP_A})
	got = proxy_connect(PORT_A)
	assert b"upstream=tcp-a" in got  # the forwarder dialed the right v6 backend


def test_forward_echoes_bytes_roundtrip():
	sync({str(PORT_A): TCP_A})
	got = proxy_connect(PORT_A, send=b"ping-12345\n")
	# Banner + echo of what we sent — bytes round-trip through the L4 pipe intact.
	assert b"upstream=tcp-a" in got
	assert b"ping-12345" in got


# --- remap without reload --------------------------------------------------


def test_remap_no_reload():
	sync({str(PORT_A): TCP_A})
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)
	pid_before = _proxy_master_pid()
	# Repoint the SAME public port at a DIFFERENT backend — a pure dict write.
	sync({str(PORT_A): TCP_B})
	assert b"upstream=tcp-b" in proxy_connect(PORT_A)
	assert _proxy_master_pid() == pid_before  # nginx never reloaded


# --- unmapped --------------------------------------------------------------


def test_unmapped_port_drops_connection():
	sync({str(PORT_A): TCP_A})  # PORT_B intentionally absent from the map
	# An unmapped-but-listening port: the connection is accepted (nginx binds the
	# whole range) then dropped by the router's ngx.exit(ERROR) — no banner.
	got = proxy_connect(PORT_B, timeout=3.0)
	assert b"upstream=" not in got


# --- bulk SYNC + canonical GET (byte-equality) -----------------------------


def test_sync_replaces_atomically():
	sync({str(PORT_A): TCP_A})  # a stale entry that must be removed
	sync({str(PORT_B): TCP_B})  # replace: A gone, B present
	assert b"upstream=tcp-b" in proxy_connect(PORT_B)
	assert b"upstream=" not in proxy_connect(PORT_A, timeout=3.0)


def test_get_map_is_canonical_json():
	sync({str(PORT_B): TCP_B, str(PORT_A): TCP_A})
	live = stream_admin("GET")
	expected = json.dumps({str(PORT_A): TCP_A, str(PORT_B): TCP_B}, sort_keys=True, indent=2) + "\n"
	assert live == expected  # byte-identical to the Atlas-side canonical_json


# --- restart reload (persistence) ------------------------------------------


def test_restart_reloads_from_stream_mapjson():
	sync({str(PORT_A): TCP_A})
	stream_admin("DUMP")  # force the snapshot now
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	_wait_for_stream_admin()
	# No admin calls after restart — the `ports` dict repopulated from stream-map.json.
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


# ===========================================================================
# Expanded coverage — robustness, concurrency, durability, observability.
# The originals above prove the happy path; everything below pins the failure
# modes and the subtler behaviors so a regression there can't ship silently —
# the L4 mirror of test_proxy.py's expanded HTTP suite. Only the behaviors that
# MEAN something at L4 are here (no Host/headers/TLS/branded-page analogues).
# ===========================================================================


# --- malformed SYNC body: reject, leave the live map untouched -------------


def test_sync_malformed_body_rejected_without_corrupting_map():
	# A scalar, garbage, or a typed-wrong object must be rejected and leave the live
	# map exactly as it was — stream_admin.lua validates the WHOLE body before
	# mutating (the L4 twin of admin.lua's /sync guard). The reply starts with
	# "error"; the seeded entry survives every rejected sync.
	sync({str(PORT_A): TCP_A})  # seed a known-good entry
	before = stream_admin("GET")
	# Newline-terminate each body exactly as the controller's canonical_json does, so
	# the typed-validation path (not the framing-incompleteness path) is exercised:
	# a scalar / array / number-value must each be rejected as a non-object.
	for bad in ("42\n", '"x"\n', "[1,2]\n", '["a","b"]\n', '{"10000": 5}\n'):
		reply = stream_admin("SYNC", bad)
		assert reply.startswith("error"), f"{bad!r} unexpectedly accepted ({reply!r})"
	# The map is byte-identical to before any rejected sync — no partial/garbage write.
	assert stream_admin("GET") == before
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)  # and still forwards


def test_sync_incomplete_body_rejected():
	# Two distinct rejection diagnostics, keyed on whether the accumulated bytes
	# DECODE to a non-table or stay un-decodable:
	#   - a complete-line scalar ("42\n" — exactly the framing canonical_json uses,
	#     always newline-terminated) decodes to a number → the typed object-shape
	#     error.
	#   - a truncated object ("{\n") never decodes to a table and the client closes →
	#     "incomplete body".
	# (A scalar with NO trailing newline, "42", is itself an incomplete line and so
	# also reports "incomplete body" — the loop never sees a complete line before EOF.
	# The controller always newline-terminates, so the typed path is the real one.)
	sync({str(PORT_A): TCP_A})
	assert "must be a JSON object" in stream_admin("SYNC", "42\n")
	assert stream_admin("SYNC", "{\n").startswith("error: incomplete body")
	# The map survived both rejected syncs.
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


def test_unknown_verb_errors_and_leaves_map_intact():
	# An unknown verb must be rejected cleanly and must not touch the map. (The
	# client uppercases argv[1], so these reach stream_admin.lua as-is.)
	sync({str(PORT_A): TCP_A})
	before = stream_admin("GET")
	for verb in ("FOO", "DELETE", "PUT"):
		assert stream_admin(verb).startswith("error: unknown verb")
	assert stream_admin("GET") == before


def test_sync_duplicate_ports_last_wins():
	# A SYNC body with a duplicate JSON key is decoded by cjson last-wins; the map
	# ends with one entry at the final value. A decoder that kept the first / both
	# would forward to the wrong backend.
	body = '{"%s": "%s", "%s": "%s"}\n' % (PORT_A, TCP_A, PORT_A, TCP_B)
	assert stream_admin("SYNC", body).startswith("ok")
	live = json.loads(stream_admin("GET"))
	assert live == {str(PORT_A): TCP_B}  # last value won, single entry
	assert b"upstream=tcp-b" in proxy_connect(PORT_A)


def test_empty_map_canonical_json():
	# An empty SYNC body yields exactly "{}\n" from GET — the canonical empty-map
	# bytes the controller's canonical_json({}) also emits.
	sync({})
	assert stream_admin("GET") == "{}\n"


# --- a bad / dead / misbehaving backend must not wedge the forwarder --------


def test_forward_unreachable_backend_fails_clean():
	# A port mapped to an in-subnet v6 with nothing listening: the ONE connection
	# fails (no banner — the SYN is dropped/refused, or proxy_connect_timeout fires)
	# and — the property that matters — every other mapped port keeps forwarding.
	sync({str(PORT_C): TCP_DEAD, str(PORT_A): TCP_A})
	assert not _banner(PORT_C, timeout=8), "dead backend unexpectedly produced a banner"
	# The live route still works right after — one dead backend didn't wedge nginx.
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


def test_forward_rst_backend_fails_clean():
	# A backend that accepts then immediately closes without sending yields no
	# banner; the forwarder must surface that as a closed connection, never a hang,
	# and keep serving other ports.
	sync({str(PORT_C): TCP_RST, str(PORT_A): TCP_A})
	assert not _banner(PORT_C, timeout=8)
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


def test_forward_silent_backend_does_not_wedge():
	# A backend that accepts then never sends a byte: the client read blocks until
	# its own deadline (no banner ever arrives), but the forwarder must not crash or
	# wedge — other ports keep serving and the master PID is unchanged.
	pid_before = _proxy_master_pid()
	sync({str(PORT_C): TCP_SILENT, str(PORT_A): TCP_A})
	assert not _banner(PORT_C, timeout=4)  # silent backend → no upstream data
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)
	assert _proxy_master_pid() == pid_before  # no reload, no restart


def test_sync_bad_backend_literal_fails_on_forward():
	# Backend literals that aren't a dialable [v6]:port — a bare v4, garbage, a
	# double-bracketed addr, an empty string — pass SYNC's string-type validation
	# (they ARE strings) but make proxy_pass fail. The forward must yield no banner,
	# never a wrong-backend hit, and must not wedge the proxy.
	for bad in ("", "10.0.0.1:22", "garbage", "[[fd00:a71a:5::c]]:7000"):
		sync({str(PORT_C): bad})
		assert not _banner(PORT_C, timeout=6), f"bad literal {bad!r} produced a banner"
	# Proxy still healthy + a good route works.
	sync({str(PORT_A): TCP_A})
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


def test_tombstone_value_drops_clean():
	# The http side stores "-" as a tombstone and serves a branded 503. L4 has no
	# branded page, so a "-" value (which the controller never sends for ports, but
	# could arrive on a hand edit) must simply fail to forward — no banner, no hang —
	# and not wedge the forwarder. Defensive-only, but pins the behavior.
	sync({str(PORT_C): "-"})
	assert not _banner(PORT_C, timeout=6)
	sync({str(PORT_A): TCP_A})
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


def test_unmapped_to_mapped_transition():
	# A port with no mapping drops the connection; mapping it (a pure dict write)
	# makes the very next connection forward — no reload between the two states.
	sync({str(PORT_A): TCP_A})  # PORT_C deliberately unmapped
	assert not _banner(PORT_C, timeout=3)
	pid_before = _proxy_master_pid()
	sync({str(PORT_C): TCP_B, str(PORT_A): TCP_A})
	assert b"upstream=tcp-b" in proxy_connect(PORT_C)
	assert _proxy_master_pid() == pid_before  # the map write reloaded nothing


# --- no reload across writes (the core invariant) --------------------------


def test_rapid_syncs_never_reload():
	# A burst of SYNCs is a burst of dict writes — nginx must never reload (the
	# reload-free property the whole pre-opened-pool design exists to get). Snap the
	# master PID, fire 10 distinct syncs, assert it's unchanged.
	pid_before = _proxy_master_pid()
	for i in range(10):
		sync({str(PORT_A): (TCP_A if i % 2 else TCP_B)})
	assert _proxy_master_pid() == pid_before
	# And the final state forwards correctly.
	sync({str(PORT_A): TCP_A})
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)


# --- concurrency: atomic SYNC under a concurrent reader --------------------


def test_concurrent_reads_during_sync_never_partial():
	# A reader hammering GET while SYNC flips between two disjoint 200-entry maps must
	# always see a COMPLETE map (old-complete or new-complete), never a half-applied
	# one — stream_admin.lua upserts desired then deletes leftovers (no flush_all
	# window). Every observed entry count must be exactly 200.
	map_a = {str(20000 + i): TCP_A for i in range(200)}
	map_b = {str(30000 + i): TCP_B for i in range(200)}
	sync(map_a)
	stop = threading.Event()
	seen_bad = []

	def reader():
		while not stop.is_set():
			try:
				n = len(json.loads(stream_admin("GET")))
			except (subprocess.CalledProcessError, json.JSONDecodeError):
				seen_bad.append("unparseable")
				continue
			if n != 200:
				seen_bad.append(n)

	t = threading.Thread(target=reader, daemon=True)
	t.start()
	try:
		for i in range(6):
			sync(map_b if i % 2 else map_a)
	finally:
		stop.set()
		t.join(timeout=5)
	assert not seen_bad, f"reader saw partial/garbage map: {seen_bad[:10]}"
	sync({})


def test_concurrent_syncs_stay_coherent():
	# Concurrent SYNCs of distinct payloads must leave the map coherent: GET is valid
	# canonical JSON, every value a string, and a routable port still forwards.
	sync({})
	payloads = [
		{str(PORT_A): TCP_A},
		{str(PORT_B): TCP_B},
		{str(PORT_A): TCP_B, str(PORT_B): TCP_A},
		{str(PORT_C): TCP_A},
	]
	with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
		list(pool.map(sync, payloads))
	live = json.loads(stream_admin("GET"))
	assert all(isinstance(v, str) for v in live.values())
	assert set(live).issubset({str(PORT_A), str(PORT_B), str(PORT_C)})
	# GET equals the on-disk dump after a forced DUMP — the canonical serializers agree.
	stream_admin("DUMP")
	on_disk = _exec_proxy("cat", "/var/lib/nginx/stream-map.json")
	assert stream_admin("GET") == on_disk
	sync({})


# --- persistence: debounce + the durability window -------------------------


def test_dump_writes_canonical_json_to_disk():
	# DUMP (the third verb, previously untested) forces a persist; the on-disk
	# stream-map.json must be byte-identical to GET — the byte-equality the
	# controller's "in sync?" reconcile compare relies on (spec principle #3).
	sync({str(PORT_B): TCP_B, str(PORT_A): TCP_A})
	assert stream_admin("DUMP").startswith("ok")
	on_disk = _exec_proxy("cat", "/var/lib/nginx/stream-map.json")
	assert on_disk == stream_admin("GET")
	expected = json.dumps({str(PORT_A): TCP_A, str(PORT_B): TCP_B}, sort_keys=True, indent=2) + "\n"
	assert on_disk == expected  # byte-identical to the Atlas-side canonical_json


def test_debounce_coalesces_burst():
	# stream_persist.schedule_dump() debounces 1s, so a burst of SYNCs coalesces into
	# FEWER disk writes than syncs. Quiesce with a forced DUMP, then fire 8 quick
	# syncs and assert the on-disk file's mtime changed FEWER than 8 times (coalesced)
	# while at least one dump eventually lands. Direction, not "exactly one" (flakes
	# on a slow box).
	sync({})
	stream_admin("DUMP")  # land a baseline
	time.sleep(1.2)  # let any pending debounce fire
	baseline_mtime = _stream_map_mtime()
	for i in range(8):
		sync({str(PORT_A + i): TCP_A})
	seen = set()
	deadline = time.time() + 4
	while time.time() < deadline:
		m = _stream_map_mtime()
		if m:
			seen.add(m)
		time.sleep(0.1)
	after = _stream_map_mtime()
	assert after and after != baseline_mtime, "burst never dumped"
	assert len(seen) < 8, f"writes did not coalesce: {len(seen)} distinct dumps for 8 syncs"
	sync({})
	stream_admin("DUMP")


def test_undumped_write_lost_on_restart():
	# The debounce is a durability WINDOW: a SYNC not yet dumped is lost if the proxy
	# restarts before the 1s timer fires (stream-map.json is the only thing reloaded).
	# Atlas's reconcile is the backstop, so this is INTENDED — pin it so the window
	# is understood, not silently widened. Control: a dumped map survives. Subject: an
	# immediately-restarted unforced write does not.
	sync({str(PORT_A): TCP_A})
	stream_admin("DUMP")  # force the durable state to disk
	# Now an unforced write, then restart FAST (before the ~1s debounce dump).
	sync({str(PORT_A): TCP_A, str(PORT_B): TCP_B})
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	_wait_for_stream_admin()
	assert b"upstream=tcp-a" in proxy_connect(PORT_A), "dumped map did not survive restart"
	# PORT_B was added after the last DUMP and lost on the fast restart.
	assert not _banner(PORT_B, timeout=3), "un-dumped write survived (debounce window widened?)"
	sync({str(PORT_A): TCP_A})
	stream_admin("DUMP")


# --- corrupt on-disk state boots clean --------------------------------------


@pytest.mark.parametrize("corrupt", ["{garbage", "42", "[1,2,3]", '{"10000":', '{"10000": 5}'])
def test_corrupt_streammap_boots_and_serves(corrupt):
	# A torn / wrong-typed stream-map.json must not crash-loop the proxy at boot:
	# stream_persist.load ignores a non-object and skips nothing it can't store; the
	# dict comes up empty (or partial) and the forwarder serves rather than failing
	# to start. _wait_for_stream_admin() is the crash-loop oracle.
	_exec_proxy("sh", "-c", f"printf '%s' {json.dumps(corrupt)} > /var/lib/nginx/stream-map.json")
	subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
	_wait_for_stream_admin()  # raises if the proxy never came back up
	# Healthy: STAT answers and a fresh SYNC + forward works.
	json.loads(stream_admin("STAT"))
	sync({str(PORT_A): TCP_A})
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)
	stream_admin("DUMP")  # leave a known-clean snapshot for the next test


# --- STAT observability verb (the L4 twin of GET /healthz) ------------------


def test_stat_reports_entries_and_last_dump():
	# STAT = entry count + last-dump epoch, symmetric to the http admin's
	# GET /healthz. After a SYNC of two ports + a forced DUMP, entries==2 and
	# last_dump is a positive epoch.
	sync({str(PORT_A): TCP_A, str(PORT_B): TCP_B})
	stream_admin("DUMP")
	stat = json.loads(stream_admin("STAT"))
	assert stat["entries"] == 2
	assert isinstance(stat["last_dump"], (int, float)) and stat["last_dump"] > 0
	sync({})


def test_stat_last_dump_advances_on_dump():
	# A fresh DUMP must move last_dump forward — the epoch tracks the most recent
	# persist (the signal an operator uses to spot a stalled persister).
	sync({str(PORT_A): TCP_A})
	stream_admin("DUMP")
	first = json.loads(stream_admin("STAT"))["last_dump"]
	time.sleep(1.1)
	sync({str(PORT_A): TCP_B})
	stream_admin("DUMP")
	second = json.loads(stream_admin("STAT"))["last_dump"]
	assert second > first, f"last_dump did not advance: {first} -> {second}"
	sync({})


# --- scale: a large map syncs fast and routes at any port -------------------


def test_large_port_map_applies_and_routes():
	# The `ports` dict is 16m (hashed, O(1) lookup). Push 5000 ports via one SYNC and
	# assert it applies within a sane time, STAT counts them, and a lookup at the
	# start/middle/end of the keyspace still forwards (one of them on a PUBLISHED port
	# so we can actually connect; the rest prove the dict scaled).
	desired = {str(11000 + i): (TCP_A if i % 2 else TCP_B) for i in range(5000)}
	desired[str(PORT_A)] = TCP_A  # a published port we can reach
	t = time.time()
	assert stream_admin("SYNC", json.dumps(desired, sort_keys=True, indent=2) + "\n").startswith("ok")
	elapsed = time.time() - t
	print(f"\n[large-map] SYNC of {len(desired)} ports took {elapsed:.2f}s")
	assert elapsed < 30, f"SYNC of {len(desired)} ports took {elapsed:.1f}s — too slow"
	assert json.loads(stream_admin("STAT"))["entries"] == len(desired)
	# The published port routes despite the large map (lookup is O(1)).
	assert b"upstream=tcp-a" in proxy_connect(PORT_A)
	sync({})


# --- bidirectional payload integrity through the L4 pipe --------------------


def test_bidirectional_large_payload_roundtrips():
	# A large payload must echo back byte-identical through the forwarder — the L4
	# pipe is transparent, no buffering/truncation/rewrite. 256 KiB both directions.
	sync({str(PORT_A): TCP_A})
	payload = bytes((i * 7 + 3) % 256 for i in range(256 * 1024))
	got = _roundtrip(PORT_A, payload, timeout=15)
	# The banner ("upstream=tcp-a\n") precedes the echo; strip it, the rest is our echo.
	assert got.startswith(b"upstream=tcp-a\n")
	echoed = got[len(b"upstream=tcp-a\n") :]
	assert echoed == payload, f"echo mismatch: sent {len(payload)}B, got {len(echoed)}B back"


# --- latency: per-connection forward overhead is bounded --------------------


def test_forward_latency_bounded():
	# A regression guard, not a benchmark: the L4 connect→banner path is a single
	# dict read + a dial. Median over N connects must stay well under a generous
	# docker ceiling — a blow-up would mean a reload-per-connection or a linear scan
	# crept in. Prints the real numbers; asserts only direction.
	sync({str(PORT_A): TCP_A})
	# Warm up the backend connect path.
	for _ in range(3):
		proxy_connect(PORT_A)
	samples = []
	for _ in range(30):
		t = time.time()
		got = proxy_connect(PORT_A)
		samples.append(time.time() - t)
		assert b"upstream=tcp-a" in got
	samples.sort()
	median = samples[len(samples) // 2]
	print(f"\n[forward-latency] n={len(samples)} median={median * 1000:.1f}ms max={samples[-1] * 1000:.1f}ms")
	assert median < 0.5, f"forward median {median * 1000:.0f}ms too high (reload/scan crept in?)"


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


def _banner(port: int, timeout: float = 5.0) -> bytes:
	"""Connect to a published proxy port and return the upstream banner (the first
	line a well-behaved backend sends). Returns b"" if the connection is refused,
	reset, or yields no data within `timeout` — i.e. the port did NOT reach a live
	backend. Used by the dead/misbehaving/unmapped tests, where the assertion is
	"no banner" rather than a specific status."""
	try:
		with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
			sock.settimeout(timeout)
			chunks = []
			try:
				while True:
					data = sock.recv(4096)
					if not data:
						break
					chunks.append(data)
					if b"\n" in b"".join(chunks):
						break
			except TimeoutError:
				pass
			return b"".join(chunks)
	except (ConnectionRefusedError, ConnectionResetError, OSError, TimeoutError):
		return b""


def _roundtrip(port: int, payload: bytes, timeout: float = 15.0) -> bytes:
	"""Send `payload` and read back banner + the full echo, looping recv until we've
	received at least len(banner)+len(payload) bytes or the deadline passes. Used by
	the bidirectional-integrity test where the echo can span many TCP segments."""
	deadline = time.time() + timeout
	with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
		sock.settimeout(timeout)
		sock.sendall(payload)
		chunks = []
		got = 0
		# The banner is short ("upstream=tcp-a\n"); we want it + the whole echo back.
		want = len(payload) + len(b"upstream=tcp-a\n")
		try:
			while got < want and time.time() < deadline:
				data = sock.recv(65536)
				if not data:
					break
				chunks.append(data)
				got += len(data)
		except TimeoutError:
			pass
		return b"".join(chunks)


def _exec_proxy(*argv: str) -> str:
	"""Run a command inside the proxy container (for inspecting/seeding on-disk
	state) and return its stdout."""
	return subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", *argv],
		cwd=HERE,
		capture_output=True,
		text=True,
		check=True,
	).stdout


def _stream_map_mtime() -> float | None:
	"""The mtime (epoch seconds) of /var/lib/nginx/stream-map.json inside the proxy
	container, or None if the file is absent. A change in mtime == a dump landed; the
	debounce test counts distinct mtimes to prove a burst coalesced."""
	# %.Y is the mtime with a fractional (nanosecond) part, so sub-second dumps are
	# distinguishable — the debounce test needs finer than 1s granularity.
	res = subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", "stat", "-c", "%.Y", "/var/lib/nginx/stream-map.json"],
		cwd=HERE,
		capture_output=True,
		text=True,
		check=False,
	)
	if res.returncode != 0:
		return None
	try:
		return float(res.stdout.strip())
	except ValueError:
		return None
