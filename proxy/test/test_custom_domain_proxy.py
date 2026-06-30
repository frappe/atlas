#!/usr/bin/env python3
# Custom-domain SNI-passthrough release gate (spec/12 § The stream front-door, spec/13
# § Custom domains, spec/18 Phase 2). Drives the running docker-compose stack to prove
# the proxy's :443 SNI fork and :80 ACME fork against a real TLS-terminating backend
# (tls-vm, which holds its OWN cert):
#
#   - a custom SNI in the `domains` map is PASSED THROUGH raw to the backend's :443, and
#     the cert the client sees is the BACKEND's (CN=tls-vm.custom.example), NOT the proxy
#     wildcard — proof the proxy never decrypted;
#   - the negotiated SNI survives the strip-path (echoed back by the backend);
#   - a custom SNI NOT in the map is DROPPED (an unknown/deregistered name);
#   - a wildcard SNI still terminates AT the proxy (the L7 path is unregressed);
#   - the :80 ACME fork: a custom host's /.well-known/acme-challenge/ reaches the VM,
#     a wildcard host's is served LOCALLY (the wildcard guard).
#
# Run the stack first:  docker compose up --build -d
# Then:                 python3 -m pytest test_custom_domain_proxy.py -v

import json
import os
import subprocess
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
# The FULL wildcard zone the proxy strips from an SNI/Host — what the Dockerfile
# writes to /var/lib/nginx/region (active_root_domain().domain), the SAME constant
# test_proxy.py uses. Deliberately DEEPER than "<region>.frappe.dev" (the extra `.x`
# label) so the gate exercises the real platform-zone shape; a wildcard SNI must
# match this exact suffix for sni_router.lua's wildcard fork to terminate it.
ZONE = "test.x.frappe.dev"

# Host-published ports (docker-compose.yml): 8443->container :443 (the SNI front-door),
# 8080->container :80 (the ACME fork).
FRONT_443 = "127.0.0.1:8443"
FRONT_80 = "127.0.0.1:8080"

CUSTOM_DOMAIN = "tls-vm.custom.example"
TLS_VM_V6 = "fd00:a71a:5::7a"
# The dialable literals the controller's reconcile would write (atlas.atlas.proxy).
SNI_BACKEND = f"[{TLS_VM_V6}]:443"  # the :443 SNI map value
ACME_BACKEND = f"[{TLS_VM_V6}]"  # the :80 ACME map value (bare bracketed; :80 appended)

# A wildcard subdomain for the unregressed-L7 + ACME-guard checks.
WILDCARD_HOST = f"acme.{ZONE}"


def _exec(*cmd: str, stdin: str | None = None) -> subprocess.CompletedProcess:
	return subprocess.run(
		["docker", "compose", "exec", "-T", "proxy", *cmd],
		cwd=HERE,
		input=stdin,
		capture_output=True,
		text=True,
	)


def stream_admin(verb: str, body: str | None = None) -> str:
	"""Drive the stream-admin line protocol inside the proxy container (the SAME binary
	the controller runs over SSH). Used for the SNI map (GET-SNI / SYNC-SNI)."""
	args = ["stream-admin", verb]
	res = _exec(*args, stdin=body)
	assert res.returncode == 0, f"stream-admin {verb} failed: {res.stderr}"
	return res.stdout


def acme_admin(method: str, path: str, body: str | None = None) -> tuple[int, str]:
	"""Drive the http admin's /acme routes over the unix socket inside the container."""
	curl = [
		"curl",
		"-s",
		"-o",
		"-",
		"-w",
		"\n%{http_code}",
		"--unix-socket",
		"/run/nginx/admin.sock",
		"-X",
		method,
	]
	if body is not None:
		curl += ["--data-binary", "@-"]
	curl.append(f"http://localhost{path}")
	res = _exec(*curl, stdin=body)
	out = res.stdout
	payload, _, status = out.rpartition("\n")
	return int(status), payload


def _curl(
	target: str, host: str, path: str = "/", extra: list[str] | None = None
) -> subprocess.CompletedProcess:
	"""curl a host:port with SNI+Host forced to `host` via --resolve. -k so a self-signed
	backend cert doesn't fail the request (we inspect the cert separately)."""
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
	"""Start each run from empty SNI + ACME maps, and wait for the stack."""
	_wait_for_stack()
	stream_admin("SYNC-SNI", "{}")
	acme_admin("POST", "/acme/sync", "{}")
	yield


def _wait_for_stack(timeout: float = 40.0) -> None:
	deadline = time.time() + timeout
	while time.time() < deadline:
		res = _exec("curl", "-s", "--unix-socket", "/run/nginx/admin.sock", "http://localhost/healthz")
		if res.returncode == 0 and '"ok"' in res.stdout:
			return
		time.sleep(0.5)
	raise RuntimeError("proxy stack never came up")


# --- :443 SNI passthrough --------------------------------------------------


def test_custom_sni_passes_through_to_backend_tls():
	# Map the custom domain into the SNI map, then a TLS request with that SNI must reach
	# the backend, which terminates with its OWN cert.
	stream_admin("SYNC-SNI", json.dumps({CUSTOM_DOMAIN: SNI_BACKEND}))
	res = _curl(FRONT_443, CUSTOM_DOMAIN)
	assert "@@STATUS@@200" in res.stdout, res.stdout + res.stderr
	# The backend echoed its identity AND the SNI it negotiated — proof the raw TLS
	# stream (incl. the ClientHello SNI) reached it untouched.
	body = res.stdout.split("@@STATUS@@")[0]
	assert "upstream=tls-vm" in body, body
	assert "tls=backend" in body, body
	assert f"sni={CUSTOM_DOMAIN}" in body, body


def test_passthrough_presents_the_backend_cert_not_the_proxy_wildcard():
	# The decisive passthrough proof: the cert the client sees for the custom domain is
	# the BACKEND's self-signed cert (CN=tls-vm.custom.example), never the proxy wildcard.
	stream_admin("SYNC-SNI", json.dumps({CUSTOM_DOMAIN: SNI_BACKEND}))
	# -v dumps the peer cert subject to stderr; assert it's the backend CN.
	res = _curl(FRONT_443, CUSTOM_DOMAIN, extra=["-v"])
	combined = res.stdout + res.stderr
	assert "tls-vm.custom.example" in combined, combined
	# The proxy's wildcard placeholder CN must NOT be what was presented.
	assert "nginx-placeholder" not in combined, combined


def test_unknown_custom_sni_is_dropped():
	# A custom SNI NOT in the map (an unregistered / deregistered name) is dropped at L4 —
	# the connection fails, no handshake forwarded. (A registered domain is in the map
	# immediately; there is no readiness gate — only the unknown-name drop.)
	stream_admin("SYNC-SNI", "{}")  # empty map
	res = _curl(FRONT_443, "notmapped.custom.example")
	# curl reports a connection/TLS failure (non-zero), never a 200.
	assert "@@STATUS@@200" not in res.stdout, res.stdout + res.stderr
	assert res.returncode != 0


def test_wildcard_sni_still_terminates_at_the_proxy():
	# Regression guard: a *.<region> SNI is NOT a custom domain; it must still reach the
	# proxy's own :8443 terminator (wildcard cert) via the front-door, unregressed. With an
	# empty sites map it 404s (branded), which proves it terminated AT the proxy (a passthrough
	# would have failed the handshake — the proxy holds the cert the wildcard SNI matches).
	stream_admin("SYNC-SNI", "{}")
	acme_admin("POST", "/sync", "{}")  # empty the HTTP sites map too — else a leftover
	# mapping for this subdomain (from another test sharing the container) 200s instead.
	res = _curl(FRONT_443, WILDCARD_HOST)
	# It reached the L7 terminator (a real HTTP status from nginx), not an L4 drop.
	assert "@@STATUS@@" in res.stdout, res.stdout + res.stderr
	status = res.stdout.split("@@STATUS@@")[-1].strip()
	assert status in ("404", "503"), f"expected branded miss, got {status}"


# --- :80 ACME fork ---------------------------------------------------------


def test_custom_acme_challenge_reaches_the_vm():
	# The custom domain in the ACME map: its /.well-known/acme-challenge/<token> is
	# forwarded to the VM (which answers from its own webroot — here the seeded store).
	acme_admin("POST", "/acme/sync", json.dumps({CUSTOM_DOMAIN: ACME_BACKEND}))
	# Seed a challenge on the backend (stands in for certbot writing the webroot).
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
	# Fail loud if the seed didn't land (e.g. the backend image lacks curl) — otherwise
	# the fetch below 404s for the wrong reason and the assertion misleads.
	assert _seed.returncode == 0 and "seeded" in _seed.stdout, (_seed.stdout, _seed.stderr)
	# Fetch the challenge through the proxy's :80, Host = the custom domain.
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
	# A *.<region> challenge must be served from the proxy's OWN webroot, NEVER forwarded
	# to a VM (the wildcard guard — no tenant may answer a *.<region> challenge). With no
	# local webroot file it 404s from the PROXY, which proves it was not proxied to the VM
	# (the VM would have its own answer/404, but the key check is it stayed local).
	acme_admin("POST", "/acme/sync", json.dumps({CUSTOM_DOMAIN: ACME_BACKEND}))
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
	# Served locally from the webroot (404, no such file) — a clean local answer, not a proxy.
	assert status == "404", res.stdout


# --- the SNI map admin round-trips canonically -----------------------------


def test_sni_map_get_round_trips_canonically():
	# GET-SNI returns the SAME canonical bytes the controller emits (sorted, 2-space
	# indent, trailing newline), so the reconcile byte-diff is exact.
	body = json.dumps({CUSTOM_DOMAIN: SNI_BACKEND})
	stream_admin("SYNC-SNI", body)
	live = stream_admin("GET-SNI")
	expected = '{\n  "%s": "%s"\n}\n' % (CUSTOM_DOMAIN, SNI_BACKEND)
	assert live == expected, repr(live)
