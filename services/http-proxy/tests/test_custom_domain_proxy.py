#!/usr/bin/env python3
"""Release gate for the custom-domain SNI passthrough and the ACME fork.

The tests drive the docker-compose stack against tls-vm, a backend holding its own
certificate, and cover both forks: the raw passthrough on port 443, the branded
page for an unknown name, the layer 4 drop with no SNI, the wildcard name that
still terminates here, and the ACME challenge that reaches the VM.

	docker compose -f docker/docker-compose.yml up --build -d
	python3 -m pytest test_custom_domain_proxy.py -v
"""

import json
import subprocess
import time

import pytest

from helpers import TEST_DIR, compose_exec, unix_http

HERE = TEST_DIR
# The same constant test_proxy.py uses. A wildcard SNI must match this exact
# suffix, or sni_router.lua does not fork it to the terminator.
ZONE = "test.x.frappe.dev"

# Published by docker-compose.yml: 8443 to the front door, 8080 to the ACME fork.
FRONT_443 = "127.0.0.1:8443"
FRONT_80 = "127.0.0.1:8080"

CUSTOM_DOMAIN = "tls-vm.custom.example"
TLS_VM_V6 = "fd00:a71a:5::7a"
# The API stores the VM address once. The HTTP and SNI routers add their own port.
SNI_BACKEND = TLS_VM_V6
ACME_BACKEND = TLS_VM_V6

WILDCARD_HOST = f"acme.{ZONE}"


def _exec(*cmd: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return compose_exec("proxy", *cmd, stdin=stdin, check=False)


def sync_domains(body: str = "{}") -> None:
    status, response = admin("POST", "/v1/domains/sync", body)
    assert status == 200, response


def get_domains() -> str:
    status, response = admin("GET", "/v1/domains")
    assert status == 200, response
    return response


def admin(method: str, path: str, body: str | None = None) -> tuple[int, str]:
    """Call the HTTP admin socket."""
    return unix_http("proxy", "/run/nginx/admin.sock", method, path, body, check=False)


def _curl(
    target: str, host: str, path: str = "/", extra: list[str] | None = None
) -> subprocess.CompletedProcess:
    """Call host:port with the SNI and Host forced. -k accepts a self-signed cert."""
    ip, _, port = target.partition(":")
    cmd = [
        "curl",
        "-sk",
        "-D",
        "/dev/stderr",
        "-w",
        "\n@@STATUS@@%{http_code}",
        "--resolve",
        f"{host}:{port}:{ip}",
        f"https://{host}:{port}{path}",
    ]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture(scope="module", autouse=True)
def clean_maps():
    """Start each run from empty SNI and ACME maps."""
    _wait_for_stack()
    admin("POST", "/v1/sites/sync", "{}")
    sync_domains("{}")
    yield


def _wait_for_stack(timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _exec(
            "curl",
            "-s",
            "--unix-socket",
            "/run/nginx/admin.sock",
            "http://localhost/v1/healthz",
        )
        if res.returncode == 0 and '"ok"' in res.stdout:
            return
        time.sleep(0.5)
    raise RuntimeError("proxy stack never came up")




def test_custom_sni_passes_through_to_backend_tls():
    """The backend echoes its name and the SNI it negotiated, thus the raw TLS
    stream and its ClientHello arrived unchanged.
    """
    sync_domains(json.dumps({CUSTOM_DOMAIN: SNI_BACKEND}))
    res = _curl(FRONT_443, CUSTOM_DOMAIN)
    assert "@@STATUS@@200" in res.stdout, res.stdout + res.stderr
    body = res.stdout.split("@@STATUS@@")[0]
    assert "upstream=tls-vm" in body, body
    assert "tls=backend" in body, body
    assert f"sni={CUSTOM_DOMAIN}" in body, body


def test_passthrough_presents_the_backend_cert_not_the_proxy_wildcard():
    """The decisive proof of a passthrough: the client sees the backend certificate.

    "Frappe Cloud" is the O field of the placeholder, thus that text here would mean
    the proxy terminated instead of passing the stream through.
    """
    sync_domains(json.dumps({CUSTOM_DOMAIN: SNI_BACKEND}))
    res = _curl(FRONT_443, CUSTOM_DOMAIN, extra=["-v"])
    combined = res.stdout + res.stderr
    assert "tls-vm.custom.example" in combined, combined
    assert "Frappe Cloud" not in combined, combined


def test_unknown_custom_sni_serves_the_unconfigured_page():
    """An unregistered name terminates on the placeholder and gets a branded 404.

    -k walks past the expected warning, thus the test sees what a person sees after
    clicking through. Every registered domain enters the map at once, with no
    readiness gate, thus only an unknown name reaches this fork.
    """
    sync_domains("{}")
    res = _curl(FRONT_443, "notmapped.custom.example", extra=["-v"])
    assert "@@STATUS@@404" in res.stdout, res.stdout + res.stderr
    body = res.stdout.split("@@STATUS@@")[0]
    assert "Domain not configured" in body, body
    combined = res.stdout + res.stderr
    assert "Frappe Cloud" in combined, combined


def test_empty_sni_is_dropped_at_l4():
    """Forcing an empty SNI is not portable, thus probe by IP with no --resolve
    name: curl sends no SNI for an IP literal.
    """
    sync_domains("{}")
    res = subprocess.run(
        ["curl", "-sk", "-w", "\n@@STATUS@@%{http_code}", f"https://{FRONT_443}/"],
        capture_output=True,
        text=True,
    )
    assert "@@STATUS@@200" not in res.stdout, res.stdout + res.stderr
    assert res.returncode != 0


def test_wildcard_sni_still_terminates_at_the_proxy():
    """A regression guard on the unchanged layer 7 path.

    With an empty sites map it brands a 404, which proves it terminated HERE: a
    passthrough would have failed the handshake, since only the proxy holds the
    certificate the wildcard SNI matches.
    """
    sync_domains("{}")
    # Empty the HTTP sites map also. A mapping that another test left gives a 200.
    admin("POST", "/v1/sites/sync", "{}")
    res = _curl(FRONT_443, WILDCARD_HOST)
    assert "@@STATUS@@" in res.stdout, res.stdout + res.stderr
    status = res.stdout.split("@@STATUS@@")[-1].strip()
    assert status in ("404", "503"), f"expected branded miss, got {status}"




def test_custom_acme_challenge_reaches_the_vm():
    """The VM answers from its own store, which stands in for a webroot."""
    admin("POST", "/v1/domains/sync", json.dumps({CUSTOM_DOMAIN: ACME_BACKEND}))
    token, value = "tok-custom", "the-key-authz"
    _seed = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "tls-vm",
            "curl",
            "-s",
            f"http://localhost/__seed/{token}/{value}",
        ],
        cwd=HERE,
        capture_output=True,
        text=True,
    )
    # Fail here if the store did not take the value. If not, the request below gives
    # a 404 for the wrong reason.
    assert _seed.returncode == 0 and "seeded" in _seed.stdout, (
        _seed.stdout,
        _seed.stderr,
    )
    # Get the challenge through the port 80 of the proxy with the custom domain as
    # the Host header.
    res = subprocess.run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            "--resolve",
            f"{CUSTOM_DOMAIN}:8080:127.0.0.1",
            f"http://{CUSTOM_DOMAIN}:8080/.well-known/acme-challenge/{token}",
        ],
        capture_output=True,
        text=True,
    )
    body, _, status = res.stdout.rpartition("\n")
    assert status == "200", res.stdout
    assert value in body, body


def test_wildcard_acme_challenge_is_served_locally_not_proxied():
    """The wildcard guard: no tenant may answer a challenge for the wildcard zone.

    The local webroot holds no such file, thus a 404 from the PROXY proves it stayed
    local and was never forwarded.
    """
    admin("POST", "/v1/domains/sync", json.dumps({CUSTOM_DOMAIN: ACME_BACKEND}))
    res = subprocess.run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            "--resolve",
            f"{WILDCARD_HOST}:8080:127.0.0.1",
            f"http://{WILDCARD_HOST}:8080/.well-known/acme-challenge/whatever",
        ],
        capture_output=True,
        text=True,
    )
    _, _, status = res.stdout.rpartition("\n")
    assert status == "404", res.stdout


def test_custom_domain_plain_http_reaches_the_vm():
    """Port 80 forwards a custom domain to its VM, like any other host.

    Only an ACME challenge for the wildcard zone is intercepted. The VM decides for
    itself whether to redirect the visitor to HTTPS.
    """
    admin("POST", "/v1/domains/sync", json.dumps({CUSTOM_DOMAIN: ACME_BACKEND}))
    res = subprocess.run(
        [
            "curl",
            "-s",
            "-D",
            "/dev/stderr",
            "-w",
            "\n@@STATUS@@%{http_code}",
            "--resolve",
            f"{CUSTOM_DOMAIN}:8080:127.0.0.1",
            f"http://{CUSTOM_DOMAIN}:8080/",
        ],
        capture_output=True,
        text=True,
    )
    assert "@@STATUS@@200" in res.stdout, res.stdout + res.stderr
    body = res.stdout.split("@@STATUS@@")[0]
    assert "upstream=tls-vm" in body, body
    assert "tls=plain" in body, "port 80 must reach the VM without TLS"
    assert "location:" not in res.stderr.lower(), (
        f"port 80 redirected instead of forwarding: {res.stderr!r}"
    )


def test_targeted_domain_patch_and_delete_update_both_paths():
    """One domain API mutation updates plaintext and passthrough routing."""
    status, body = admin(
        "PATCH",
        f"/v1/domains/{CUSTOM_DOMAIN}",
        json.dumps({"address": TLS_VM_V6}),
    )
    assert status == 200 and json.loads(body)["address"] == TLS_VM_V6

    plain = subprocess.run(
        [
            "curl", "-s", "--resolve", f"{CUSTOM_DOMAIN}:8080:127.0.0.1",
            f"http://{CUSTOM_DOMAIN}:8080/",
        ],
        capture_output=True,
        text=True,
    )
    assert "upstream=tls-vm" in plain.stdout
    assert "tls=plain" in plain.stdout

    secure = _curl(FRONT_443, CUSTOM_DOMAIN)
    assert "upstream=tls-vm" in secure.stdout
    assert "tls=backend" in secure.stdout

    status, _ = admin("DELETE", f"/v1/domains/{CUSTOM_DOMAIN}")
    assert status == 204
    assert "Domain not configured" in _curl(
        FRONT_443, CUSTOM_DOMAIN, extra=["-v"]
    ).stdout




def test_sni_map_get_round_trips_canonically():
    body = json.dumps({CUSTOM_DOMAIN: SNI_BACKEND})
    sync_domains(body)
    live = get_domains()
    expected = '{\n  "%s": "%s"\n}\n' % (CUSTOM_DOMAIN, SNI_BACKEND)
    assert live == expected, repr(live)
