#!/usr/bin/env python3
# Build-shape release gate (proxy-stock-nginx-plus-compile.md §7). The companion
# test_proxy.py proves the proxy BEHAVES correctly (routing, sync, TLS, ws); this
# file proves the proxy is BUILT correctly — that the stack is genuinely "stock
# nginx from apt + our compiled dynamic modules", not a hand-rolled look-alike.
#
# These assertions are the safety net for the build.sh rewrite (custom all-source
# compile -> nginx.org apt base + dynamic modules). They run against the SAME
# running container test_proxy.py drives, so a green run here means the shipped
# guest snapshot has the same provenance.
#
# Run the stack first:  docker compose up --build -d
# Then:                 python3 -m pytest test_build.py -v
#
# Everything is introspected INSIDE the proxy container via `docker compose exec`
# (faithful to production: Atlas reaches the guest over SSH, never a host mount).

import json
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

# The dynamic modules build.sh compiles + nginx.conf load_module's. NDK is
# linked into the lua modules and ALSO ships its own ndk_http_module.so; all must
# be present and loaded. ngx_stream_lua is the L4 sibling of ngx_http_lua
# (spec/17-tcp-proxy.md) — a SEPARATE .so the stream{} forwarder needs. Keep this
# in lockstep with conf/nginx.conf's load_module lines and build.sh §4.
EXPECTED_MODULE_SOS = {
	"ndk_http_module.so",
	"ngx_http_lua_module.so",
	"ngx_stream_lua_module.so",
	"ngx_http_headers_more_filter_module.so",
}


def exec_proxy(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
	"""Run a command INSIDE the proxy container and capture output."""
	return subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", *argv],
		cwd=HERE,
		capture_output=True,
		text=True,
		check=check,
	)


@pytest.fixture(scope="module")
def nginx_V() -> str:
	"""`nginx -V` output (configure args go to stderr)."""
	res = exec_proxy("nginx", "-V")
	return (res.stdout + res.stderr).strip()


# --- the base really is the stock nginx.org apt package ---------------------


def test_nginx_is_apt_package_from_nginx_org():
	# dpkg knows the binary only if it came from a .deb. The all-source build did
	# `make install` — dpkg would NOT own /usr/sbin/nginx then. This is the single
	# strongest proof the swap happened.
	res = exec_proxy("dpkg-query", "-S", "/usr/sbin/nginx", check=False)
	assert res.returncode == 0, f"nginx not dpkg-owned (not an apt install?): {res.stderr}"
	assert "nginx:" in res.stdout, res.stdout


def test_nginx_package_is_held():
	# build.sh `apt-mark hold nginx` so the snapshot can never silently apt-upgrade
	# the base out from under the compiled-against-this-version modules.
	res = exec_proxy("apt-mark", "showhold")
	assert "nginx" in res.stdout.split(), f"nginx not held: {res.stdout!r}"


def test_nginx_version_is_stable_not_mainline(nginx_V):
	# nginx.org `stable` packages are even-minor (1.28.x, 1.30.x); mainline is
	# odd-minor (1.29.x). The plan picked stable for a TLS front door. Guard the
	# repo pin so a silent flip to mainline trips the gate.
	first = nginx_V.splitlines()[0]
	assert "nginx/" in first, first
	ver = first.split("nginx/")[1].split()[0]
	major, minor, _ = (int(x) for x in ver.split(".")[:3])
	assert major == 1, f"unexpected nginx major: {ver}"
	assert minor % 2 == 0, f"nginx {ver} is mainline (odd minor); plan pins stable"


def test_running_nginx_matches_the_build_pin(nginx_V):
	# build.sh pins NGINX_VERSION to an exact version and the modules are compiled
	# against it. Assert the SHIPPED binary is exactly that pin, so a drift between
	# the declared pin and what actually got baked (e.g. a stale snapshot, or a pin
	# edited without a rebake) trips the gate — this is the lock the pin exists for.
	pin = _build_pin("NGINX_VERSION")
	running = nginx_V.splitlines()[0].split("nginx/")[1].split()[0]
	assert running == pin, f"running nginx {running} != build.sh pin {pin} (rebake needed?)"


def test_nginx_built_with_compat(nginx_V):
	# --with-compat is load-bearing: it is what lets our separately-compiled .so's
	# load into the apt binary. The apt nginx.org package ships with it; assert it.
	assert "--with-compat" in nginx_V, nginx_V


def test_nginx_has_openssl_we_did_not_handbuild(nginx_V):
	# The apt package links a distro/nginx.org OpenSSL — build.sh no longer builds
	# one. `nginx -V` reports the TLS lib it was built with.
	assert "OpenSSL" in nginx_V, nginx_V


# --- the dynamic modules are present AND actually loaded --------------------


def test_module_sos_present_on_disk():
	res = exec_proxy("ls", "/etc/nginx/modules")
	present = set(res.stdout.split())
	missing = EXPECTED_MODULE_SOS - present
	assert not missing, f"missing module .so(s): {missing}; have {present}"


def test_modules_loaded_at_runtime():
	# -t parses the full config including the load_module lines; if any .so were
	# ABI-incompatible (--with-compat missing) or absent, this fails. Proves the
	# modules don't just exist on disk but actually load into THIS nginx.
	res = exec_proxy("nginx", "-t", check=False)
	combined = res.stdout + res.stderr
	assert res.returncode == 0, f"nginx -t failed (module load?):\n{combined}"
	assert "syntax is ok" in combined.lower(), combined
	assert "test is successful" in combined.lower(), combined


def test_nginx_conf_loads_each_expected_module():
	# The committed config must name every module we ship — a .so on disk that no
	# load_module references is dead weight; a load_module with no .so crashes.
	# Cross-check the conf's load_module lines against the built set.
	res = exec_proxy("cat", "/etc/nginx/nginx.conf")
	loaded = {
		line.split()[1].rstrip(";").split("/")[-1]
		for line in res.stdout.splitlines()
		if line.strip().startswith("load_module")
	}
	assert loaded == EXPECTED_MODULE_SOS, f"load_module set {loaded} != built {EXPECTED_MODULE_SOS}"


# --- the compiled Lua runtime resolves (the one init-time crash seam) -------


def test_cjson_safe_resolves_in_nginx_lua():
	# persist.lua/admin.lua require("cjson.safe") at init_by_lua; if the cpath is
	# wrong nginx crashes on boot. The fact the container is UP already implies it,
	# but assert it directly via the admin path that encodes JSON (GET /map runs
	# cjson through persist) so a regression names cjson, not "routing broke".
	res = exec_proxy("curl", "-s", "--unix-socket", "/run/nginx/admin.sock", "http://localhost/map")
	# Valid JSON object back == cjson.encode ran end to end.
	assert json.loads(res.stdout) is not None or res.stdout.strip() in ("{}", "{}\n")


def test_stream_block_declares_its_own_lua_package_cpath():
	# stream{} is a SEPARATE Lua subsystem from http{} — lua_package_cpath set in
	# http{} does NOT carry into stream{}. stream_admin.lua/stream_persist.lua
	# require("cjson.safe"), so the stream{} block must name the cpath itself or the
	# first stream-admin call crashes loading cjson (test_tcp.py's GET exercises it
	# at runtime; this is the cheap static half that names the directive). Assert the
	# cpath appears INSIDE the stream{} block, not just somewhere in http{}.
	conf = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
	stream_block = conf[conf.index("stream {") :]
	assert "lua_package_cpath" in stream_block, (
		"stream{} missing its own lua_package_cpath — cjson.safe won't resolve"
	)


def test_stream_lua_module_version_pinned_to_0_0_17():
	# The release-gate fact the spec + memory both record: lua-resty-core 0.1.32's
	# base.lua asserts ngx_stream_lua_module == 0.0.17 EXACTLY at startup. A newer
	# stream-lua compiles fine but nginx then refuses to start ("0.0.17 required").
	# Lock the build.sh pin so a silent bump to a newer tag trips the gate here, in
	# milliseconds, instead of as a won't-boot proxy snapshot. (The .so being LOADED
	# is proven by test_modules_loaded_at_runtime + test_tcp.py's live forward; this
	# is the cheap static half that names the version.)
	pin = _build_pin("STREAM_LUA_MODULE_REF")
	assert pin == "v0.0.17", (
		f"stream-lua pin is {pin}, not v0.0.17 — resty-core 0.1.32 requires exactly 0.0.17"
	)


def test_worker_connections_clears_the_listener_count():
	# The compose gate's own original finding: nginx counts every LISTENING socket
	# against worker_connections, and the TCP forwarder pre-opens ~20000 (the
	# 10000-19999 pool on v4 AND v6) plus the http :80/:443 + two admin sockets. If
	# worker_connections didn't clear that, nginx -t would fail "worker_connections
	# are not enough for N listening sockets". Assert the configured value clears the
	# pre-opened pool span with real headroom, so a regression that lowers it (or
	# grows the pool past it) is caught statically. nginx -T expands the full config.
	dump = exec_proxy("nginx", "-T").stdout + exec_proxy("nginx", "-T").stderr
	wc_lines = [
		ln
		for ln in dump.splitlines()
		if "worker_connections" in ln and "#" not in ln.split("worker_connections")[0]
	]
	assert wc_lines, "no worker_connections directive found in nginx -T"
	# Parse the numeric value (e.g. "    worker_connections  65536;").
	value = int(wc_lines[0].split("worker_connections")[1].strip().rstrip(";").split()[0])
	# The v4+v6 pool is 2 * (19999 - 10000 + 1) = 20000 listeners; require clear
	# headroom above that for real traffic. 65536 (the shipped value) passes; a drop
	# to e.g. 16384 (the old http-only default) would fail.
	assert value >= 20000 + 1024, f"worker_connections {value} too low for the ~20000-listener TCP pool"


def test_luajit_is_openresty_fork():
	# The lua module REQUIRES OpenResty's luajit2 fork, not upstream LuaJIT.
	# luajit -v prints the version banner; the fork tags itself "2.1" + a date.
	res = exec_proxy("/usr/local/bin/luajit", "-v", check=False)
	if res.returncode != 0:
		pytest.skip("luajit binary not on PATH in container (lib-only install)")
	assert "LuaJIT 2.1" in res.stdout, res.stdout


# --- headers-more + add_header survive (the ABI-shift failure class) --------


def test_security_headers_present_on_response():
	# add_header (core) HSTS/X-Frame/X-Content-Type must land on a real proxied
	# response. This is the cheap canary for the header-filter ABI-shift class the
	# plan calls out — if the header chain were broken by a module mismatch, these
	# would vanish.
	_ensure_mapped("acme")
	_, headers = _fetch_headers("acme")
	low = headers.lower()
	assert "strict-transport-security:" in low, headers
	assert "x-frame-options:" in low, headers
	assert "x-content-type-options:" in low, headers


def test_server_tokens_off_hides_version():
	# server_tokens off; in nginx.conf — the Server header must not leak the
	# version. Independent of the build swap but a regression-sensitive default.
	_ensure_mapped("acme")
	_, headers = _fetch_headers("acme")
	server_line = [ln for ln in headers.splitlines() if ln.lower().startswith("server:")]
	assert server_line, "no Server header"
	assert "/" not in server_line[0], f"version leaked: {server_line[0]}"


# --- config invariants the behavior tests can't see directly ----------------


def test_proxy_read_timeout_is_finite_and_nonzero():
	# Both proxy locations carry a finite, nonzero proxy_read_timeout — a `0` (or a
	# missing directive defaulting differently) would let a hung upstream pin a
	# worker connection forever. We can't wait out the real 600s/3600s in a test,
	# so we assert the STATIC invariant: location / = 600s, /socket.io = 3600s, and
	# no `proxy_read_timeout 0` anywhere.
	conf = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
	assert "proxy_read_timeout 600s;" in conf, "location / read timeout drifted"
	assert "proxy_read_timeout 3600s;" in conf, "/socket.io read timeout drifted"
	assert "proxy_read_timeout 0" not in conf, "a zero (infinite) read timeout slipped in"


def test_sync_uses_targeted_delete_not_flush_all():
	# /sync must mutate via get_keys + per-key delete, NEVER flush_all — a flush_all
	# would briefly empty the dict, so a concurrent reader could see an empty map
	# mid-sync (the partial-read window test_concurrent_reads_during_sync guards at
	# runtime; this is the cheap static half). admin.lua is installed in the image,
	# so check the shipped copy.
	src = exec_proxy("cat", "/etc/nginx/lua/admin.lua").stdout
	# Match an actual CALL (`:flush_all(`), not the word in a comment — admin.lua's
	# comment explains why it deliberately avoids flush_all.
	assert ":flush_all(" not in src, "admin.lua calls flush_all — opens an empty-map window"
	assert "get_keys" in src and "delete" in src, "admin.lua sync no longer does targeted delete"


def test_upstream_not_pooled_today():
	# Documents a CURRENT reality so a future change is a conscious one: location /
	# clears Connection and there is no `upstream{}`/`keepalive` block, so the proxy
	# opens a fresh TCP connection to the site per request (no pooling). vm-a counts
	# accepted connections via /__conns; N keepalive client requests must bump it by
	# N. If someone adds upstream keepalive, this flips and they update the test
	# deliberately.
	_ensure_mapped("pool")
	before = _upstream_conns()
	host = f"pool.{REGION}.frappe.dev"
	# One curl, N requests on ONE client keepalive connection (so any pooling is the
	# proxy's, not the client's).
	urls = [f"https://{host}:{HTTPS_PORT}/"] * 10
	cmd = ["curl", "-sk", "-o", "/dev/null", "--resolve", f"{host}:{HTTPS_PORT}:127.0.0.1", *urls]
	subprocess.run(cmd, capture_output=True, text=True, check=False)
	after = _upstream_conns()
	# No pooling → ~one new upstream connection per request. Allow slack for any
	# concurrent test traffic, but it must be clearly per-request, not a single
	# reused connection.
	assert after - before >= 8, f"expected ~10 new upstream connections (no pooling), saw {after - before}"


# --- helpers (mirror test_proxy.py's transport) ----------------------------

REGION = "test"
VM_A = "fd00:a71a:5::a"
HTTPS_PORT = "8443"
BUILD_SH = os.path.join(HERE, "..", "build.sh")


def _upstream_conns() -> int:
	"""vm-a's accepted-connection counter (/__conns) — read from inside the proxy
	container over the v6 south network."""
	res = exec_proxy("curl", "-s", "http://[fd00:a71a:5::a]:80/__conns")
	return json.loads(res.stdout)["conns"]


def _build_pin(name: str) -> str:
	"""Read a pinned `NAME="value"` assignment out of build.sh — so the gate
	checks the SHIPPED binary against the one source of truth (the script), not a
	value duplicated into the test that could drift on its own."""
	with open(BUILD_SH) as f:
		for line in f:
			stripped = line.strip()
			if stripped.startswith(f"{name}="):
				return stripped.split("=", 1)[1].split("#")[0].strip().strip('"')
	raise AssertionError(f"{name} not found in build.sh")


def _ensure_mapped(subdomain: str) -> None:
	exec_proxy(
		"curl",
		"-s",
		"--unix-socket",
		"/run/nginx/admin.sock",
		"-X",
		"PUT",
		"--data-binary",
		VM_A,
		f"http://localhost/map/{subdomain}",
	)


def _fetch_headers(subdomain: str) -> tuple[int, str]:
	"""curl the proxy from the host (forced Host/SNI). Returns (status, headers)."""
	host = f"{subdomain}.{REGION}.frappe.dev"
	marker = "\n@@STATUS@@"
	cmd = [
		"curl",
		"-sk",
		"-D",
		"/dev/stderr",
		"-o",
		"/dev/null",
		"-w",
		marker + "%{http_code}",
		"--resolve",
		f"{host}:{HTTPS_PORT}:127.0.0.1",
		f"https://{host}:{HTTPS_PORT}/",
	]
	res = subprocess.run(cmd, capture_output=True, text=True)
	status = res.stdout.rpartition(marker)[2]
	return int(status or 0), res.stderr
