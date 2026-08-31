#!/usr/bin/env python3
"""Release gate for the HTTP behavior of the proxy image.

The tests drive the running docker-compose stack: they write mappings through the
admin socket, make HTTPS requests with a forced Host and SNI, and examine the
routing, the remap, the sync, the restart, the TLS and the websocket paths.

	docker compose -f docker/docker-compose.yml up --build -d
	python3 -m pytest test_proxy.py -v
	docker compose -f docker/docker-compose.yml down -v

The tests use curl and not a Python HTTP client, because curl gives the unix
socket, the --resolve option and HTTP/2 with one tool. This is also the transport
that the controller uses.
"""

import json
import subprocess
import threading
import time

import pytest

from helpers import TEST_DIR, compose_exec, unix_http

HERE = TEST_DIR
ADMIN_SOCK = "/run/nginx/admin.sock"

HTTPS = "127.0.0.1:8443"
HTTP = "127.0.0.1:8080"
# ZONE is the full wildcard zone the proxy strips, and what the region file holds.
# It carries one label more than "<region>.frappe.dev" on purpose: a flat zone hides
# the fault where the Lua rebuilt it as region .. ".frappe.dev".
REGION = "test"
ZONE = "test.x.frappe.dev"
VM_A = "fd00:a71a:5::a"
VM_B = "fd00:a71a:5::b"


def admin(method: str, path: str, body: str | None = None) -> tuple[int, str]:
    """Call the HTTP admin socket in the proxy container."""
    return unix_http("proxy", ADMIN_SOCK, method, path, body)


def set_site(key: str, address: str) -> tuple[int, str]:
    """Create or replace one site mapping."""
    body = json.dumps({"address": address})
    return admin("PATCH", f"/v1/sites/{key}", body)


def fetch(
    subdomain: str,
    path: str = "/",
    scheme: str = "https",
    http2: bool = False,
    extra: list[str] | None = None,
) -> tuple[int, str, str]:
    """Call the proxy with the Host and the SNI set to <subdomain>.<ZONE>.

    Returns the status, the body and the headers.
    """
    host = f"{subdomain}.{ZONE}"
    target = HTTPS if scheme == "https" else HTTP
    ip, _, port = target.partition(":")
    marker = "\n@@STATUS@@"
    cmd = ["curl", "-sk", "-D", "/dev/stderr", "-w", marker + "%{http_code}"]
    if http2:
        cmd.append("--http2")
    # The URL must carry the same port, or --resolve does not key-match.
    cmd += ["--resolve", f"{host}:{port}:{ip}", f"{scheme}://{host}:{port}{path}"]
    if extra:
        cmd += extra
    res = subprocess.run(cmd, capture_output=True, text=True)
    body, _, status = res.stdout.rpartition(marker)
    return int(status or 0), body, res.stderr


def terminator(host: str, path: str = "/", container: str = "proxy") -> tuple[str, str]:
    """Call the wildcard TLS terminator directly, bypassing the SNI front door.

    It binds loopback and needs the PROXY header, thus only a call from inside the
    container with --haproxy-protocol reaches it.
    """
    # The marker must not start with "@": -w reads "@file" as a file name.
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
    out = subprocess.run(
        cmd, cwd=HERE, capture_output=True, text=True, check=True
    ).stdout
    body, _, status = out.rpartition(marker)
    return status.strip(), body


@pytest.fixture(scope="module", autouse=True)
def clean_map():
    """Start each run of this module with an empty map."""
    _wait_for_socket()
    admin("POST", "/v1/sites/sync", "{}")
    yield


def _wait_for_socket(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = admin("GET", "/v1/healthz")
            if status == 200:
                return
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.5)
    raise RuntimeError("proxy admin socket never came up")




def test_routing_preserves_host():
    set_site("acme", VM_A)
    status, body, _ = fetch("acme")
    assert status == 200
    assert "upstream=vm-a" in body
    assert "host=acme.test.x.frappe.dev" in body


def test_multi_subdomain_one_vm():
    set_site("acme", VM_A)
    set_site("widgets", VM_A)
    for sub in ("acme", "widgets"):
        status, body, _ = fetch(sub)
        assert status == 200 and "upstream=vm-a" in body




def test_remap_no_reload():
    set_site("acme", VM_A)
    assert "upstream=vm-a" in fetch("acme")[1]
    pid_before = _proxy_master_pid()
    set_site("acme", VM_B)
    status, body, _ = fetch("acme")
    assert status == 200 and "upstream=vm-b" in body
    assert _proxy_master_pid() == pid_before




def test_unmapped_serves_branded_404():
    admin("POST", "/v1/sites/sync", "{}")
    status, body, _ = fetch("nope")
    assert status == 404
    assert "Site not found" in body


def test_tombstone_serves_503():
    """"-" means suspended, thus "preparing" and not "no such site"."""
    set_site("paused", "-")
    status, body, _ = fetch("paused")
    assert status == 503
    assert "Site not found" in body


def test_no_region_suffix_serves_404():
    """A host outside the zone gives the branded 404, never a 500."""
    set_site("acme", VM_A)
    assert terminator("acme.wrongregion.example.com")[0] == "404"




def test_bulk_sync_replaces_atomically():
    set_site("stale", VM_A)
    desired = json.dumps({"acme": VM_A, "widgets": VM_B}, sort_keys=True, indent=2)
    admin("POST", "/v1/sites/sync", desired)
    assert "upstream=vm-a" in fetch("acme")[1]
    assert "upstream=vm-b" in fetch("widgets")[1]
    assert fetch("stale")[0] == 404


def test_get_map_is_canonical_json():
    admin("POST", "/v1/sites/sync", json.dumps({"b": VM_B, "a": VM_A}))
    _, live = admin("GET", "/v1/sites")
    expected = json.dumps({"a": VM_A, "b": VM_B}, sort_keys=True, indent=2) + "\n"
    assert live == expected




def test_patch_then_get_then_delete_single():
    status, _ = set_site("solo", VM_A)
    assert status == 200
    status, body = admin("GET", "/v1/sites/solo")
    assert status == 200 and json.loads(body)["address"] == VM_A
    status, _ = admin("DELETE", "/v1/sites/solo")
    assert status == 204
    assert admin("GET", "/v1/sites/solo")[0] == 404
    assert fetch("solo")[0] == 404


def test_patch_empty_address_rejected():
    status, body = set_site("blank", "")
    assert status == 400
    assert "empty" in body.lower()


def test_v1_patch_and_delete_update_one_site():
    """Targeted updates must not require sending the regional map."""
    before = json.loads(admin("GET", "/v1/healthz")[1])["entries"]
    status, body = admin(
        "PATCH", "/v1/sites/targeted", json.dumps({"address": VM_A})
    )
    assert status == 200 and json.loads(body)["address"] == VM_A
    assert json.loads(admin("GET", "/v1/healthz")[1])["entries"] == before + 1
    assert fetch("targeted")[0] == 200

    status, body = admin(
        "PATCH", "/v1/sites/targeted", json.dumps({"address": VM_B})
    )
    assert status == 200 and json.loads(body)["address"] == VM_B
    assert "upstream=vm-b" in fetch("targeted")[1]

    status, _ = admin("DELETE", "/v1/sites/targeted")
    assert status == 204
    assert json.loads(admin("GET", "/v1/healthz")[1])["entries"] == before
    assert fetch("targeted")[0] == 404


def test_sync_malformed_body_rejected_without_corrupting_map():
    """cjson decodes a JSON array to a Lua table, thus [1,2] would inject numeric
    subdomains if admin.lua did not type-check each entry. An empty array is taken
    as an empty map, because a Lua empty table is indistinguishable from {}.
    """
    set_site("keepme", VM_A)
    for bad in ('"x"', "42", "not json", "[1,2]", '["a","b"]'):
        status, _ = admin("POST", "/v1/sites/sync", bad)
        assert status == 400, f"{bad!r} unexpectedly accepted ({status})"
    status, body = admin("GET", "/v1/sites/keepme")
    assert status == 200 and json.loads(body)["address"] == VM_A
    admin("POST", "/v1/sites/sync", "{}")


def test_unknown_admin_route_404s():
    status, body = admin("GET", "/nope")
    assert status == 404
    assert "unknown route" in body.lower()




def test_healthz_reports_entries_and_last_dump():
    admin("POST", "/v1/sites/sync", json.dumps({"acme": VM_A, "widgets": VM_B}))
    admin("POST", "/v1/dump")
    status, body = admin("GET", "/v1/healthz")
    assert status == 200
    health = json.loads(body)
    assert health["ok"] is True
    assert health["entries"] == 2
    assert isinstance(health["last_dump"], (int, float)) and health["last_dump"] > 0




def test_restart_reloads_from_mapjson():
    admin("POST", "/v1/sites/sync", json.dumps({"acme": VM_A}))
    admin("POST", "/v1/dump")
    subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
    _wait_for_socket()
    status, body, _ = fetch("acme")
    assert status == 200 and "upstream=vm-a" in body




def test_plain_http_proxies_to_the_vm():
    """Port 80 terminates nothing and forwards to the VM in plain text.

    The proxy must not redirect to HTTPS. A site that wants that redirect issues it
    itself, exactly as it would if it faced the internet directly.
    """
    set_site("acme", VM_A)
    status, body, headers = fetch("acme", scheme="http")
    assert status == 200, headers
    assert "upstream=vm-a" in body
    assert "xfproto=http" in body, "the VM must see it was reached in plain text"
    assert "location:" not in headers.lower(), (
        f"port 80 redirected instead of forwarding: {headers!r}"
    )


def test_plain_http_miss_serves_branded_page():
    """A miss on port 80 gets the same branded page the TLS path serves."""
    admin("POST", "/v1/sites/sync", "{}")
    status, _, _ = fetch("nope", scheme="http")
    assert status == 404




def test_http2_negotiated():
    set_site("acme", VM_A)
    status, _, headers = fetch("acme", http2=True)
    assert status == 200
    assert "http/2" in headers.lower().splitlines()[0]




def test_socketio_upgrade():
    """A websocket upgrade needs HTTP/1.1, because HTTP/2 has no Upgrade header.

    --max-time limits the wait. The 101 handshake arrives immediately, and the
    connection then stays open. curl reports the status that it already received.
    """
    set_site("acme", VM_A)
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




def test_dead_upstream_does_not_wedge_proxy():
    """One dead upstream must fail its own request and wedge nothing else.

    Status 0 means curl hit --max-time first on a dropped SYN.
    """
    set_site("dead", "fd00:a71a:5::dead")
    status, _, _ = fetch("dead", extra=["--max-time", "8"])
    assert status in (0, 502, 504), (
        f"dead upstream gave {status}, expected gateway error/timeout"
    )
    set_site("acme", VM_A)
    assert "upstream=vm-a" in fetch("acme")[1]




def test_tls11_refused():
    """--tls-max stops curl from falling back up to a permitted version."""
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
    assert res.stdout.strip() in ("", "000"), (
        f"TLS1.1 unexpectedly accepted: {res.stdout!r}"
    )
    assert res.returncode != 0, "curl should fail the handshake"


# ===========================================================================
# The tests above examine the correct path. The tests below examine the
# behaviors and the failure modes: bad input, bad upstreams and bad state.
# ===========================================================================




def test_query_string_preserved():
    """proxy_pass carries no URI part, thus nginx forwards $request_uri verbatim.

    Frappe leans on ?cmd= and ?page= everywhere. curl strips a real fragment client
    side, thus no test asserts on one.
    """
    set_site("acme", VM_A)
    raw_path = "/app/foo?cmd=bar&page=2&q=a%20b"
    status, body, _ = fetch("acme", path=raw_path)
    assert status == 200
    assert "upstream=vm-a" in body
    assert f"path={raw_path}" in body


def test_xff_realip_proto_injected():
    """A Frappe site needs X-Forwarded-Proto to know it was reached over TLS.

    The addresses are docker bridge addresses, thus only their presence is checked.
    """
    set_site("acme", VM_A)
    status, body, _ = fetch("acme")
    assert status == 200
    assert "xfproto=https" in body
    xff = _echoed(body, "xff")
    xrealip = _echoed(body, "xrealip")
    assert xff, f"X-Forwarded-For not injected: {body!r}"
    assert xrealip, f"X-Real-IP not injected: {body!r}"


def test_connection_header_cleared_non_ws():
    """Connection is hop-by-hop: leaking it would break upstream keepalive."""
    set_site("acme", VM_A)
    status, body, _ = fetch("acme", extra=["-H", "Connection: close"])
    assert status == 200
    assert _echoed(body, "conn") == "", (
        f"client Connection leaked to upstream: {body!r}"
    )


def test_host_case_insensitive_routes():
    """Host names are case-insensitive, thus the routing must be too."""
    set_site("acme", VM_A)
    # Upper-case the whole host. The suffix must stay the active zone, or router.lua
    # cannot strip it: the subject here is the case, not the zone.
    status, body, _ = fetch("acme", extra=["-H", f"Host: {('acme.' + ZONE).upper()}"])
    assert status == 200
    assert "upstream=vm-a" in body
    assert f"host=acme.{ZONE}" in body


def test_sni_host_mismatch_routes_by_host():
    set_site("acme", VM_A)
    set_site("widgets", VM_B)
    host_sni = f"acme.{ZONE}"
    host_hdr = f"widgets.{ZONE}"
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
    assert "upstream=vm-b" in out, out
    assert f"host={host_hdr}" in out




def test_socketio_plain_get_routes_and_misses():
    """The two proxy locations share one router and one map, not two.

    test_socketio_upgrade only proves the 101 handshake.
    """
    admin("POST", "/v1/sites/sync", "{}")
    set_site("acme", VM_A)
    status, body, _ = fetch("acme", path="/socket.io/EIO=4")
    assert status == 200 and "upstream=vm-a" in body
    assert "path=/socket.io/EIO=4" in body
    status, body, _ = fetch("nope", path="/socket.io/")
    assert status == 404 and "Site not found" in body



SEC_HEADERS = {
    "strict-transport-security": "max-age=63072000; includesubdomains; preload",
    "x-frame-options": "sameorigin",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def test_security_headers_full_values_on_200():
    """The exact values, so a typo'd max-age trips the gate. test_build.py only
    checks that the headers are present.
    """
    set_site("acme", VM_A)
    _, _, headers = fetch("acme")
    low = headers.lower()
    for name, value in SEC_HEADERS.items():
        assert f"{name}: {value}" in low, f"{name} value drift: {headers!r}"


def test_security_headers_on_branded_404_and_503():
    """Without `always` the headers vanish on every response that is not a 2xx."""
    admin("POST", "/v1/sites/sync", "{}")
    _, _, h404 = fetch("nope")
    set_site("paused", "-")
    _, _, h503 = fetch("paused")
    for label, headers in (("404", h404), ("503", h503)):
        low = headers.lower()
        for name in SEC_HEADERS:
            assert f"{name}:" in low, f"{name} missing on branded {label}: {headers!r}"


def test_branded_page_content_type():
    admin("POST", "/v1/sites/sync", "{}")
    for sub, addr, want in (("nope", None, 404), ("paused", "-", 503)):
        if addr:
            set_site(sub, addr)
        status, _, headers = fetch(sub)
        assert status == want
        assert "content-type: text/html; charset=utf-8" in headers.lower(), headers


def test_branded_page_terminal_no_cycle():
    admin("POST", "/v1/sites/sync", "{}")
    fetch("nope")
    fetch("paused-x", path="/socket.io/")
    res = exec_proxy_text(
        "grep",
        "-c",
        "rewrite or internal redirection cycle",
        "/var/log/nginx/error.log",
        check=False,
    )
    count = res.stdout.strip() or "0"
    assert count == "0", f"redirect cycle in error.log: {count}"




def test_acme_challenge_served_not_redirected():
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
    assert "location:" not in res.stderr.lower(), (
        f"acme challenge was redirected: {res.stderr!r}"
    )


def test_default_server_handles_bare_ip_host():
    """Such a name carries no zone-matching SNI, thus the public 443 drops it at
    layer 4 and it can only be probed at the terminator.
    """
    set_site("acme", VM_A)
    for host in ("127.0.0.1", ZONE):
        assert terminator(host)[0] == "404", (
            f"bare-IP host {host!r} did not get branded 404"
        )


def test_front_door_forks_unroutable_sni():
    """The two miss cases split: a NAMED miss terminates on the placeholder
    certificate and gets the branded 404 (-k walks past the expected warning), while
    an SNI-less connection is dropped at layer 4 with no handshake at all.
    """
    set_site("acme", VM_A)

    def front(host: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "curl",
                "-sk",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--resolve",
                f"{host}:{HTTPS.rpartition(':')[2]}:127.0.0.1",
                f"https://{host}:{HTTPS.rpartition(':')[2]}/",
            ],
            capture_output=True,
            text=True,
        )

    for host in ("acme.wrongregion.example.com", ZONE):
        res = front(host)
        assert res.returncode == 0, f"{host!r} did not complete (rc={res.returncode})"
        assert res.stdout.strip() == "404", (
            f"{host!r} got HTTP {res.stdout.strip()}, expected branded 404"
        )

    res = front("127.0.0.1")
    assert res.returncode != 0, (
        f"bare IP unexpectedly completed (rc=0, status={res.stdout.strip()})"
    )
    assert res.stdout.strip() in ("000", ""), (
        f"bare IP got HTTP {res.stdout.strip()}, expected an L4 drop"
    )




def test_get_map_sub_404_shape():
    """The controller distinguishes the two bodies."""
    admin("POST", "/v1/sites/sync", "{}")
    status, body = admin("GET", "/v1/sites/ghost")
    assert status == 404 and json.loads(body)["error"] == "no such site"
    status, body = admin("GET", "/nope")
    assert status == 404 and "unknown route" in body.lower()


def test_method_dispatch_405_vs_404():
    """A known route with an unhandled method gives 405; an unknown route gives 404.

    A single mapping takes GET, PATCH and DELETE; the collection takes GET and PUT.
    Everything else is a 405.
    """
    for method in ("POST", "PUT"):
        status, body = admin(method, "/v1/sites/acme")
        assert status == 405, f"{method} /v1/sites/acme gave {status}"
        assert "method not allowed" in body.lower(), body
    for method in ("POST", "PATCH", "DELETE"):
        status, body = admin(method, "/v1/sites")
        assert status == 405, f"{method} /v1/sites gave {status}"
        assert "method not allowed" in body.lower(), body
    status, body = admin("GET", "/v1/nope")
    assert status == 404 and "unknown route" in body.lower()


def test_admin_wrong_method_route_combos():
    combos = [
        ("DELETE", "/v1/healthz"),
        ("PUT", "/v1/sites/sync"),
        ("DELETE", "/v1/sites"),
        ("POST", "/v1/sites/acme"),
        ("GET", "/v1/dump"),
    ]
    for method, route in combos:
        status, _ = admin(method, route)
        assert status in (404, 405), f"{method} {route} gave {status}, want 404/405"




def test_patch_trailing_whitespace_stripped():
    """Whitespace around an address is ignored."""
    set_site("ws", VM_A + "\n  ")
    status, body = admin("GET", "/v1/sites/ws")
    assert status == 200 and json.loads(body)["address"] == VM_A
    assert "upstream=vm-a" in fetch("ws")[1]


def test_patch_only_whitespace_rejected():
    """A blank mapping would make the router build http://[]:80."""
    status, body = set_site("blank2", "   \n\t")
    assert status == 400 and "empty" in body.lower()
    assert admin("GET", "/v1/sites/blank2")[0] == 404




def test_empty_address_routes_clean_not_200():
    """An empty address passes the type check of /v1/sites/sync but is nonsense."""
    admin("POST", "/v1/sites/sync", json.dumps({"empty": ""}))
    status, body, _ = fetch("empty", extra=["--max-time", "8"])
    assert status == 0 or status >= 500, f"empty addr gave {status}"
    assert "upstream=" not in body
    set_site("acme", VM_A)
    assert "upstream=vm-a" in fetch("acme")[1]
    assert admin("GET", "/v1/healthz")[0] == 200


def test_non_v6_address_fails_clean():
    """router.lua brackets any address blindly, thus this guards operator error."""
    for bad in ("1.2.3.4", "garbage", f"[{VM_A}]"):
        set_site("badaddr", bad)
        status, body, _ = fetch("badaddr", extra=["--max-time", "8"])
        assert status == 0 or status >= 500, f"addr {bad!r} gave {status}"
        assert "upstream=" not in body, f"addr {bad!r} reached an upstream"
    set_site("acme", VM_A)
    assert "upstream=vm-a" in fetch("acme")[1]


def test_misbehaving_upstream_502_not_crash():
    """vm-bad picks its failure mode from the forwarded Host. See misbehave.py."""
    set_site("garbage", VM_BAD)
    status, body, _ = fetch("garbage", extra=["--max-time", "8"])
    assert status == 0 or status >= 500, f"garbage upstream gave {status}"
    assert "upstream=" not in body
    # The truncated mode sends a correct status line but a short body, thus the read
    # of the client fails even when a status arrives.
    set_site("truncated", VM_BAD)
    rc = fetch_rc("truncated", extra=["--max-time", "8"])
    assert rc != 0, "truncated upstream should fail the client transfer"
    set_site("acme", VM_A)
    assert "upstream=vm-a" in fetch("acme")[1]




def test_weird_host_headers_degrade():
    """The SNI stays valid and only the Host changes, thus the request reaches the
    proxy. A leading dot strips to an empty subdomain and an IPv4 literal has no
    zone suffix, so both brand a 404; a raw IPv6 literal is an invalid Host for the
    parser of nginx, which rejects it with a 400 before the Lua runs.
    """
    set_site("acme", VM_A)
    sni = f"acme.{ZONE}"
    expect = {
        f".{ZONE}": ("404",),
        "192.0.2.7": ("404",),
        # An unbracketed IPv6 literal is not a valid Host. nginx either rejects it
        # with a 400 or hands it to the router, which brands it. Both degrade cleanly.
        "fd00:a71a:5::1": ("400", "404"),
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
    assert admin("GET", "/v1/healthz")[0] == 200


def test_weird_subdomain_keys_literal():
    """A future key with a quote or a backslash must not corrupt map.json."""
    admin("POST", "/v1/sites/sync", "{}")
    weird = {
        "sub.with.dots": VM_A,
        "quote%22key": VM_A,  # %22 is a quote character
        "back%5Cslash": VM_B,  # %5C is a backslash
    }
    for key, addr in weird.items():
        status, _ = set_site(key, addr)
        assert status == 200, key
    _, body = admin("GET", "/v1/sites")
    live = json.loads(body)
    assert live.get("sub.with.dots") == VM_A
    assert live.get('quote"key') == VM_A
    assert live.get("back\\slash") == VM_B
    admin("POST", "/v1/sites/sync", "{}")


def test_sync_duplicate_keys_last_wins():
    """cjson is last-wins. A decoder that kept the first would route wrong."""
    body = '{"dup": "%s", "dup": "%s"}' % (VM_A, VM_B)
    status, _ = admin("POST", "/v1/sites/sync", body)
    assert status == 200
    _, got = admin("GET", "/v1/sites/dup")
    assert json.loads(got)["address"] == VM_B
    _, full = admin("GET", "/v1/sites")
    assert list(json.loads(full).keys()).count("dup") == 1
    admin("POST", "/v1/sites/sync", "{}")




def test_large_sync_body_spills_and_applies():
    """Past the in-memory limit read_body spills to a file. It must still apply."""
    desired = {f"s{i}": (VM_A if i % 2 else VM_B) for i in range(8000)}
    status, _ = admin("POST", "/v1/sites/sync", json.dumps(desired))
    assert status == 200
    _, health = admin("GET", "/v1/healthz")
    assert json.loads(health)["entries"] == 8000
    assert "upstream=vm-a" in fetch("s4243")[1]
    admin("POST", "/v1/sites/sync", "{}")




def test_concurrent_reads_during_sync_never_partial():
    """admin.lua upserts then deletes leftovers, thus there is no empty window.

    The two maps hold 200 disjoint keys each. During a sync both sets are briefly
    present, so the count rises toward 400 and only a count BELOW 200 means the
    reader saw a torn map.
    """
    map_a = {f"a{i}": VM_A for i in range(200)}
    map_b = {f"b{i}": VM_B for i in range(200)}
    admin("POST", "/v1/sites/sync", json.dumps(map_a))
    stop = threading.Event()
    seen_bad = []

    def reader():
        while not stop.is_set():
            _, body = admin("GET", "/v1/sites")
            n = len(json.loads(body))
            if n < 200:
                seen_bad.append(n)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for i in range(6):
            admin("POST", "/v1/sites/sync", json.dumps(map_b if i % 2 else map_a))
    finally:
        stop.set()
        t.join(timeout=5)
    assert not seen_bad, f"reader saw a torn map: {seen_bad[:10]}"
    admin("POST", "/v1/sites/sync", "{}")


def test_concurrent_crud_stays_coherent():
    """Concurrent writes must leave the map coherent and equal to the dumped file."""
    admin("POST", "/v1/sites/sync", "{}")
    ops = [
        lambda: admin("POST", "/v1/sites/sync", json.dumps({"a": VM_A, "b": VM_B, "c": VM_A})),
        lambda: set_site("d", VM_B),
        lambda: admin("DELETE", "/v1/sites/a"),
        lambda: set_site("e", VM_A),
    ]
    threads = [threading.Thread(target=op) for op in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    _, body = admin("GET", "/v1/sites")
    live = json.loads(body)
    assert all(isinstance(v, str) for v in live.values())
    assert set(live).issubset({"a", "b", "c", "d", "e"})
    assert admin("GET", "/v1/healthz")[0] == 200
    admin("POST", "/v1/dump")
    _, served = admin("GET", "/v1/sites")
    on_disk = exec_proxy_text("cat", "/var/lib/nginx/map.json").stdout
    assert served == on_disk, "GET /v1/sites != on-disk map.json after dump"
    admin("POST", "/v1/sites/sync", "{}")




def test_debounce_coalesces_burst():
    """A burst is persisted by one or more delayed dumps, not one per update."""
    admin("POST", "/v1/sites/sync", "{}")
    previous = _last_dump() or 0
    admin("POST", "/v1/dump")
    baseline = _wait_for_dump(previous)
    for i in range(8):
        set_site(f"burst{i}", VM_A)
    seen = set()
    deadline = time.time() + 6
    while time.time() < deadline:
        ld = _last_dump()
        if ld:
            seen.add(round(ld, 3))
        time.sleep(0.1)
    after = _last_dump()
    assert after and after > baseline, (
        f"burst never dumped (baseline={baseline}, after={after})"
    )
    assert len(seen) < 8, (
        f"writes did not coalesce: {len(seen)} distinct dumps for 8 writes"
    )
    admin("POST", "/v1/sites/sync", "{}")


def test_undumped_write_lost_on_restart():
    """The debounce is a durability window: an undumped write dies on a restart.

    The reconcile of Atlas is the backstop, thus this is intended. Pin it so the
    window cannot widen silently.
    """
    admin("POST", "/v1/sites/sync", "{}")
    set_site("durable", VM_A)
    admin("POST", "/v1/dump")
    set_site("ephemeral", VM_A)
    subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
    _wait_for_socket()
    assert "upstream=vm-a" in fetch("durable")[1], (
        "dumped write did not survive restart"
    )
    assert fetch("ephemeral")[0] == 404, (
        "un-dumped write unexpectedly survived (debounce window widened?)"
    )
    admin("POST", "/v1/sites/sync", "{}")
    admin("POST", "/v1/dump")




@pytest.mark.parametrize(
    "corrupt", ["{garbage", "42", "[1,2,3]", '{"acme":', '{"acme": 5}']
)
def test_corrupt_mapjson_boots_and_serves(corrupt):
    """A torn map.json must not crash-loop the proxy at boot.

    _wait_for_socket is the crash-loop oracle.
    """
    exec_proxy_text(
        "sh", "-c", f"printf '%s' {json.dumps(corrupt)} > /var/lib/nginx/map.json"
    )
    subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
    _wait_for_socket()
    assert admin("GET", "/v1/healthz")[0] == 200
    assert fetch("nope")[0] == 404
    # A file with a string key but a value that is not a string must also fail
    # cleanly, because the router then builds an incorrect upstream.
    if corrupt == '{"acme": 5}':
        status, body, _ = fetch("acme", extra=["--max-time", "8"])
        assert status == 0 or status >= 500
        assert "upstream=" not in body
    admin("POST", "/v1/sites/sync", "{}")
    admin("POST", "/v1/dump")




def test_empty_region_first_label_fallback():
    """proxy-noregion runs the same image with an empty region file.

    This is terminator behavior: with no zone its front door cannot fork on an SNI.
    """
    noregion_set_site("acme", VM_A)
    status, body = terminator("acme.anything.example.com", container="proxy-noregion")
    assert status == "200", (status, body)
    assert "upstream=vm-a" in body, body
    # A host with no dot has no label to remove, thus it gives the branded 404.
    assert terminator("acme", container="proxy-noregion")[0] == "404"




VM_BAD = "fd00:a71a:5::bad"


def fetch_rc(subdomain: str, path: str = "/", extra: list[str] | None = None) -> int:
    """Like fetch(), but returns the exit code, for a transfer that fails."""
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
    """Read one `key=value` token from the echo line of upstream.py."""
    for tok in body.split():
        if tok.startswith(key + "="):
            return tok[len(key) + 1 :]
    return ""


def _last_dump() -> float | None:
    """The last_dump time from GET /v1/healthz, or None if there is no dump yet."""
    _, body = admin("GET", "/v1/healthz")
    return json.loads(body).get("last_dump")


def _wait_for_dump(previous: float = 0, timeout: float = 6) -> float:
    """Wait until OpenResty has completed a scheduled site-map dump."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dumped = _last_dump()
        if dumped and dumped > previous:
            return dumped
        time.sleep(0.1)
    raise AssertionError("OpenResty did not complete a map dump")


def noregion_admin(method: str, path: str, body: str | None = None) -> tuple[int, str]:
    """Call the admin socket of the proxy container that has no region."""
    return unix_http("proxy-noregion", ADMIN_SOCK, method, path, body)


def noregion_set_site(key: str, address: str) -> tuple[int, str]:
    return noregion_admin(
        "PATCH", f"/v1/sites/{key}", json.dumps({"address": address})
    )


def exec_proxy_text(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command in the proxy container to examine or to set up state."""
    return compose_exec("proxy", *argv, check=check)


def _proxy_master_pid() -> str:
    """The PID of the nginx master, which shows that there was no reload."""
    return compose_exec("proxy", "cat", "/run/nginx.pid").stdout.strip()
