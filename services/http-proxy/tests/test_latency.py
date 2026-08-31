#!/usr/bin/env python3
"""Timing and scale gate for the proxy.

These are regression guards, not benchmarks. Each test PRINTS what it measured but
asserts only generous ceilings, because a tight limit flakes on a slow machine. A
failure means something got dramatically slower - a reload crept in, buffering
turned on, the dictionary went linear - not that one request was over budget.

	docker compose -f docker/docker-compose.yml up --build -d
	python3 -m pytest test_latency.py -v
"""

import json
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from test_proxy import (
    HERE,
    ZONE,
    VM_A,
    admin,
    exec_proxy_text,
    fetch,
    set_site,
)


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile, good enough for a regression ceiling."""
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




def test_routing_overhead_bounded():
    """The Lua access phase is one dictionary read with no allocation.

    Measured against a direct hit on the same upstream. The proxy adds TLS and one
    lookup, thus a blow-up means a reload per request or a linear scan.
    """
    set_site("acme", VM_A)
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
    # One curl for each call has a fixed cost, thus the ratio is not tight.
    assert med_proxied < med_direct * 25 + 0.1, (
        f"proxy overhead {med_proxied / med_direct:.1f}x direct (median "
        f"{med_proxied * 1000:.0f}ms vs {med_direct * 1000:.0f}ms)"
    )




def test_streaming_first_byte_before_body():
    """/__stream sends "A", waits 2s, then "B". A streaming proxy delivers the first
    byte long before the body completes; a buffering one withholds everything.
    """
    set_site("acme", VM_A)
    host = f"acme.{ZONE}"
    # A curl -w string must not start with "@", because curl then reads a file. Use a
    # marker word and remove it again. The body and the times both go to stdout.
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
    print(
        f"\n[stream] starttransfer={starttransfer * 1000:.0f}ms total={total * 1000:.0f}ms body={body!r}"
    )
    assert "A" in body and "B" in body, f"stream body incomplete: {body!r}"
    assert total >= 1.8, f"total {total:.2f}s - upstream sleep(2s) not observed"
    assert (total - starttransfer) >= 1.5, (
        f"first byte at {starttransfer:.2f}s, total {total:.2f}s - proxy appears to BUFFER"
    )




def test_tls_session_resumption_works():
    """A resumed handshake is materially cheaper, thus the session cache must be live.

    s_client writes the session, then reads it back and reports "Reused".
    """
    sess = "/tmp/proxy_sess.pem"
    host = f"acme.{ZONE}"
    first = _openssl_session(host, sess_out=sess)
    assert "New, " in first or "Session-ID:" in first, (
        f"first handshake odd:\n{first[-400:]}"
    )
    second = _openssl_session(host, sess_in=sess)
    assert "Reused" in second, f"TLS1.2 session was NOT resumed:\n{second[-400:]}"




def test_concurrency_soak_zero_errors():
    """2000 routed requests: every one a correct 200, no reload, /v1/healthz still green."""
    from test_proxy import _proxy_master_pid

    set_site("load", VM_A)
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
    assert admin("GET", "/v1/healthz")[0] == 200
    admin("POST", "/v1/sites/sync", "{}")




def test_large_map_syncs_and_routes():
    """The dictionary is hashed, thus a lookup costs the same at any size.

    Probe the start, the middle and the end of a 10000-entry key space.
    """
    desired = {
        f"site{i:05d}": (VM_A if i % 2 else "fd00:a71a:5::b") for i in range(10000)
    }
    t = time.time()
    status, _ = admin("POST", "/v1/sites/sync", json.dumps(desired))
    elapsed = time.time() - t
    print(f"\n[large-map] /v1/sites/sync of 10000 entries took {elapsed:.2f}s")
    assert status == 200
    assert elapsed < 30, f"/v1/sites/sync of 10k entries took {elapsed:.1f}s - too slow"
    _, health = admin("GET", "/v1/healthz")
    assert json.loads(health)["entries"] == 10000
    for i in (1, 5000, 9999):
        sub = f"site{i:05d}"
        want = "vm-a" if i % 2 else "vm-b"
        assert f"upstream={want}" in fetch(sub)[1], f"{sub} routed wrong"
    admin("POST", "/v1/sites/sync", "{}")




def test_cold_start_route_ready_with_healthz():
    """There must be no window where /v1/healthz is green but a routed request 404s:
    init_worker loads the map before the proxy serves.
    """
    admin("POST", "/v1/sites/sync", "{}")
    set_site("acme", VM_A)
    admin("POST", "/v1/dump")
    subprocess.run(["docker", "compose", "restart", "proxy"], cwd=HERE, check=True)
    deadline = time.time() + 30
    ready_at = None
    while time.time() < deadline:
        try:
            status, body = admin("GET", "/v1/healthz")
        except subprocess.CalledProcessError:
            time.sleep(0.05)
            continue
        if status == 200 and json.loads(body).get("entries", 0) >= 1:
            ready_at = time.time()
            break
        time.sleep(0.05)
    assert ready_at, "healthz never reported the restored entry within 30s"
    status, body, _ = fetch("acme")
    assert status == 200 and "upstream=vm-a" in body, (
        f"route not ready when healthz said entries>=1: {status} {body[:80]!r}"
    )




def _direct_upstream_times(n: int) -> list[float]:
    """Time n direct hits on vm-a from inside the container - the baseline."""
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


def _openssl_session(
    host: str, sess_out: str | None = None, sess_in: str | None = None
) -> str:
    """TLS 1.2 exercises the session-ID path; TLS 1.3 uses tickets, which s_client
    reports differently. -sess_out writes the session for a later -sess_in.
    """
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
    # s_client reads the request from stdin. Send a small GET and then close, thus
    # s_client completes the handshake and stops.
    res = subprocess.run(
        ["docker", "compose", "exec", "-T", "proxy", *args],
        cwd=HERE,
        input="GET / HTTP/1.0\r\n\r\n",
        capture_output=True,
        text=True,
    )
    return res.stdout + res.stderr
