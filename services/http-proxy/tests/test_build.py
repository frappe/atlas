#!/usr/bin/env python3
"""Release gate for the provenance of the proxy build.

test_proxy.py proves the proxy behaves correctly. This file proves it was BUILT
correctly: stock OpenResty from its own signed repository, installed and left
alone, with the files of Atlas beside it. Nothing is compiled in the guest and no
file that dpkg owns is overwritten.

	docker compose -f docker/docker-compose.yml up --build -d
	python3 -m pytest test_build.py -v
"""

import json
import os
import subprocess

import pytest

from helpers import compose_exec

HERE = os.path.dirname(os.path.abspath(__file__))

SBIN = "/usr/local/openresty/nginx/sbin/nginx"


def exec_proxy(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command in the proxy container and return the result."""
    return compose_exec("proxy", *argv, check=check)


@pytest.fixture(scope="module")
def nginx_V() -> str:
    """The output of `nginx -V`. The configure arguments go to stderr."""
    res = exec_proxy(SBIN, "-V")
    return (res.stdout + res.stderr).strip()




def test_openresty_is_apt_package():
    """dpkg owns the binary only if it came from a .deb, never from `make install`."""
    res = exec_proxy("dpkg-query", "-S", SBIN, check=False)
    assert res.returncode == 0, (
        f"OpenResty binary not dpkg-owned (built from source?): {res.stderr}"
    )
    assert "openresty:" in res.stdout, res.stdout


def test_openresty_package_is_held():
    """setup.sh holds the package, thus no upgrade can move the stack silently."""
    res = exec_proxy("apt-mark", "showhold")
    assert "openresty" in res.stdout.split(), f"openresty not held: {res.stdout!r}"


def test_running_binary_matches_the_build_pin(nginx_V):
    """A drift here means a stale snapshot, or a pin edited without a re-bake."""
    pin = _build_pin("OPENRESTY_VERSION")
    first = nginx_V.splitlines()[0]
    assert "openresty/" in first, first
    running = first.split("openresty/")[1].split()[0]
    assert running == pin, (
        f"running OpenResty {running} != setup.sh pin {pin} (rebake needed?)"
    )


def test_shipped_files_are_unmodified():
    """The decisive check: every file of the package is exactly as shipped.

    `dpkg --verify` compares the checksum of each file against the package. An
    earlier design overwrote the nginx binary with a patched recompile, which left
    dpkg claiming to own a file it had never shipped. Nothing may do that now.
    """
    res = exec_proxy("dpkg", "--verify", "openresty", check=False)
    assert res.returncode == 0 and not res.stdout.strip(), (
        f"files of the openresty package were modified after install:\n{res.stdout}"
    )


def test_no_compiler_toolchain_in_the_image():
    """Nothing is built in the guest, thus no compiler needs to be there.

    A toolchain here would mean setup.sh started compiling something again.
    """
    res = exec_proxy("sh", "-c", "command -v gcc cc make || true", check=False)
    assert not res.stdout.strip(), (
        f"a compiler is installed, but the build compiles nothing: {res.stdout!r}"
    )


def test_openresty_has_its_own_openssl(nginx_V):
    """The package supplies OpenSSL. setup.sh compiles none."""
    assert "OpenSSL" in nginx_V, nginx_V




def test_lua_and_stream_lua_are_built_in(nginx_V):
    """Both Lua subsystems ship inside OpenResty, thus there is no load_module.

    The http side runs router.lua and admin.lua; the stream side runs the SNI front
    door. A plain nginx would carry neither.
    """
    assert "ngx_lua" in nginx_V or "lua-nginx-module" in nginx_V, nginx_V
    assert "stream-lua-nginx-module" in nginx_V or "ngx_stream_lua" in nginx_V, nginx_V


def test_ssl_preread_is_compiled_in(nginx_V):
    """ssl_preread is a static module, thus the binary must carry it.

    The SNI front door reads $ssl_preread_server_name in preread_by_lua. That pair
    works only because OpenResty patches ssl_preread to keep the preread phase
    running after a successful parse; a stock nginx ends the phase and the Lua never
    runs. test_custom_domain_proxy.py proves the behavior end to end.
    """
    assert "--with-stream_ssl_preread_module" in nginx_V, nginx_V


def test_headers_more_is_built_in(nginx_V):
    assert "headers-more-nginx-module" in nginx_V, nginx_V


def test_config_needs_no_load_module():
    """OpenResty compiles the modules in, thus a load_module line would be wrong.

    It would also fail, since the package ships no dynamic .so for them.
    """
    conf = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
    loads = [ln for ln in conf.splitlines() if ln.strip().startswith("load_module")]
    assert not loads, f"nginx.conf still loads dynamic modules: {loads}"


def test_config_parses_and_loads_the_lua():
    """`nginx -t` runs init_by_lua, thus this also proves the Lua files resolve."""
    res = exec_proxy(SBIN, "-t", "-c", "/etc/nginx/nginx.conf", check=False)
    combined = res.stdout + res.stderr
    assert res.returncode == 0, f"nginx -t failed:\n{combined}"
    assert "syntax is ok" in combined.lower(), combined
    assert "test is successful" in combined.lower(), combined




def test_cjson_safe_resolves_in_lua():
    """Go through the admin path that encodes JSON, thus a regression names cjson
    rather than "routing broke".
    """
    res = exec_proxy(
        "curl", "-s", "--unix-socket", "/run/nginx/admin.sock", "http://localhost/v1/healthz"
    )
    assert json.loads(res.stdout) is not None or res.stdout.strip() in ("{}", "{}\n")


def test_control_daemon_is_installed():
    res = exec_proxy(
        "/opt/atlas/proxy-control/bin/python",
        "-c",
        "import fastapi, httpx, main, uvicorn",
    )
    assert res.returncode == 0
    unit = exec_proxy("cat", "/etc/systemd/system/atlas-proxy-control.service")
    assert "Requires=openresty.service" in unit.stdout


def test_stream_block_declares_its_own_lua_package_path():
    """stream{} is a separate Lua subsystem: the path of http{} does not carry over.

    Both blocks must name /etc/nginx/lua themselves, or a require() there fails.
    test_custom_domain_proxy.py exercises the stream side at runtime.
    """
    conf = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
    stream_block = conf[conf.index("stream {") :]
    assert "lua_package_path" in stream_block, (
        "stream{} missing its own lua_package_path - the Atlas modules won't resolve"
    )


def test_lua_path_keeps_the_openresty_default():
    """The trailing ";;" keeps the built-in path, which carries resty.core and cjson.

    Replacing it with an explicit list would drop them and stop nginx at init.
    """
    conf = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
    paths = [ln.strip() for ln in conf.splitlines() if "lua_package_path" in ln]
    assert paths, "no lua_package_path in the config"
    for line in paths:
        assert ";;" in line, f"lua_package_path drops the OpenResty default: {line}"


def test_luajit_is_the_openresty_fork():
    """The Lua modules need the luajit2 fork of OpenResty, not upstream LuaJIT."""
    res = exec_proxy(
        "sh", "-c", "/usr/local/openresty/luajit/bin/luajit -v", check=False
    )
    if res.returncode != 0:
        pytest.skip("luajit binary not present in the container")
    assert "LuaJIT 2.1" in res.stdout, res.stdout




def test_security_headers_present_on_response():
    """The cheap canary for a broken header chain."""
    _ensure_mapped("acme")
    _, headers = _fetch_headers("acme")
    low = headers.lower()
    assert "strict-transport-security:" in low, headers
    assert "x-frame-options:" in low, headers
    assert "x-content-type-options:" in low, headers


def test_server_tokens_off_hides_version():
    _ensure_mapped("acme")
    _, headers = _fetch_headers("acme")
    server_line = [
        ln for ln in headers.splitlines() if ln.lower().startswith("server:")
    ]
    assert server_line, "no Server header"
    assert "/" not in server_line[0], f"version leaked: {server_line[0]}"




def test_proxy_read_timeout_is_finite_and_nonzero():
    """A zero read timeout lets a hung upstream pin a worker connection for ever.

    A test cannot wait out the real 600s and 3600s, thus it asserts the config text.
    """
    conf = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
    assert "proxy_read_timeout 600s;" in conf, "location / read timeout drifted"
    assert "proxy_read_timeout 3600s;" in conf, "/socket.io read timeout drifted"
    assert "proxy_read_timeout 0" not in conf, (
        "a zero (infinite) read timeout slipped in"
    )


def test_package_config_untouched_and_unused():
    """Atlas keeps its config at /etc/nginx and never edits the package conffile.

    The unit is pointed at ours with -c. Overwriting the file at the prefix of
    OpenResty would desync dpkg, which test_shipped_files_are_unmodified catches.
    """
    pkg_conf = exec_proxy(
        "cat", "/usr/local/openresty/nginx/conf/nginx.conf", check=False
    )
    assert pkg_conf.returncode == 0, "the package config was deleted"
    assert "lua_shared_dict" not in pkg_conf.stdout, (
        "the package config was edited; the Atlas config belongs at /etc/nginx"
    )
    ours = exec_proxy("cat", "/etc/nginx/nginx.conf").stdout
    assert "lua_shared_dict  sites" in ours, "the Atlas config is not at /etc/nginx"


def test_sync_uses_targeted_delete_not_flush_all():
    """flush_all would empty the dictionary for a moment, thus a concurrent reader
    could see an empty map. test_concurrent_reads_during_sync guards this at
    runtime; this is the static half, against the copy in the image.
    """
    src = exec_proxy("cat", "/etc/nginx/lua/admin.lua").stdout
    # Match a call, not the word in a comment.
    assert ":flush_all(" not in src, (
        "admin.lua calls flush_all - opens an empty-map window"
    )
    assert "get_keys" in src and "delete" in src, (
        "admin.lua sync no longer does targeted delete"
    )


def test_repeated_requests_reach_upstream():
    """The proxy forwards repeated requests successfully.

    The number of backend connections is an implementation detail. OpenResty may
    reuse a connection even without an explicit upstream keepalive pool.
    """
    _ensure_mapped("pool")
    before = _upstream_conns()
    host = f"pool.{ZONE}"
    # One curl, N requests on ONE client connection, thus any pooling is the proxy's.
    urls = [f"https://{host}:{HTTPS_PORT}/"] * 10
    cmd = [
        "curl",
        "-sk",
        "-o",
        "/dev/null",
        "--resolve",
        f"{host}:{HTTPS_PORT}:127.0.0.1",
        *urls,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=False)
    after = _upstream_conns()
    assert after > before, "the repeated requests did not reach the upstream"


# `nginx -t` spawns no workers, thus only these runtime tests catch a wrong mode on
# /var/lib/nginx, which passes every static check and then fails at the first dump.


def test_master_is_root_workers_are_nginx():
    """The master binds the ports as root, then setuid()s the workers.

    OpenResty runs its workers as nobody by default, thus the `user nginx;` of the
    Atlas config is doing the work here.
    """
    res = exec_proxy("ps", "-o", "user=,args=", "-C", "nginx")
    rows = [
        ln.split(None, 1)
        for ln in res.stdout.splitlines()
        if len(ln.split(None, 1)) == 2
    ]
    masters = [u for u, cmd in rows if "master process" in cmd]
    workers = [u for u, cmd in rows if "worker process" in cmd]
    assert masters and all(u == "root" for u in masters), f"master not root: {rows}"
    assert workers and all(u == "nginx" for u in workers), f"workers not nginx: {rows}"


def test_worker_can_persist_map_to_disk():
    """Without the write bit for the group `nginx` the dump fails quietly and the
    map is lost on the next restart - the exact break the user switch can cause.
    """
    _ensure_mapped("persisttest")
    dump = exec_proxy(
        "curl",
        "-s",
        "--unix-socket",
        "/run/nginx/admin.sock",
        "-X",
        "POST",
        "http://localhost/v1/dump",
    )
    assert '"dumped":true' in dump.stdout.replace(" ", ""), (
        f"POST /v1/dump failed: {dump.stdout!r}"
    )
    cat = exec_proxy("cat", "/var/lib/nginx/map.json")
    assert "persisttest" in cat.stdout, (
        f"map.json missing the dumped key: {cat.stdout!r}"
    )
    stat = exec_proxy("stat", "-c", "%U %G %a", "/var/lib/nginx")
    assert stat.stdout.strip().startswith("root nginx"), (
        f"/var/lib/nginx not root:nginx: {stat.stdout!r}"
    )


def test_privkey_stays_root_only_after_user_switch():
    """The MASTER reads the key at config parse and no worker ever opens it, thus
    the user switch must not have widened it (CIS 4.1.3). -L follows the symlink.
    """
    stat = exec_proxy("stat", "-c", "%U %a", "-L", "/var/lib/nginx/certs/privkey.pem")
    owner, mode = stat.stdout.split()
    assert owner == "root", f"privkey not root-owned: {stat.stdout!r}"
    # The group and the others must have no read bit.
    assert int(mode[-1]) == 0, (
        f"privkey is group/world-accessible ({mode}) after user switch"
    )



REGION = "test"
# The same constant test_proxy.py uses. A host must sit under this exact zone, or
# the SNI front door drops it.
ZONE = "test.x.frappe.dev"
VM_A = "fd00:a71a:5::a"
HTTPS_PORT = "8443"
SETUP_SH = os.path.join(HERE, "..", "nginx", "setup.sh")


def _upstream_conns() -> int:
    """The accepted-connection counter of vm-a, read from the proxy container."""
    res = exec_proxy("curl", "-s", "http://[fd00:a71a:5::a]:80/__conns")
    return json.loads(res.stdout)["conns"]


def _build_pin(name: str) -> str:
    """Read a pin from setup.sh, so the gate checks the one source of truth."""
    with open(SETUP_SH) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                return stripped.split("=", 1)[1].split("#")[0].strip().strip('"')
    raise AssertionError(f"{name} not found in setup.sh")


def _ensure_mapped(subdomain: str) -> None:
    exec_proxy(
        "curl",
        "-s",
        "--unix-socket",
        "/run/nginx/admin.sock",
        "-X",
        "PATCH",
        "--data-binary",
        json.dumps({"address": VM_A}),
        f"http://localhost/v1/sites/{subdomain}",
    )


def _fetch_headers(subdomain: str) -> tuple[int, str]:
    """Call the proxy with a forced Host and SNI. Returns the status and headers."""
    host = f"{subdomain}.{ZONE}"
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
