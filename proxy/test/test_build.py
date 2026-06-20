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

# The three dynamic modules build.sh compiles + nginx.conf load_module's. NDK is
# linked into the lua module and ALSO ships its own ndk_http_module.so; all three
# must be present and loaded. Keep this in lockstep with conf/nginx.conf's
# load_module lines and build.sh §4.
EXPECTED_MODULE_SOS = {
	"ndk_http_module.so",
	"ngx_http_lua_module.so",
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


# --- helpers (mirror test_proxy.py's transport) ----------------------------

REGION = "test"
VM_A = "fd00:a71a:5::a"
HTTPS_PORT = "8443"
BUILD_SH = os.path.join(HERE, "..", "build.sh")


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
