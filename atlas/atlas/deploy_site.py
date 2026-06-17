"""Per-site deploy control plane — turn a booted golden bench VM into a serving
Frappe site, and the HTTP readiness gate that proves it (Contract B).

This is the controller side of the in-guest deploy (spec/14-self-serve.md), the
seam `atlas.atlas.doctype.site.site` imports (`deploy_site`, `wait_for_http`). It
is the sibling of `atlas.atlas.bench_image.build_bench`: drive an in-guest script
over the SAME SSH-to-the-guest path (`connection_for_guest`), recording the op as
a Task row. Where `build_bench` runs the heavy, per-site-INVARIANT bake
(bench-cli + `bench init` + a baked `site.local`, brought up + frozen serving),
this runs the ONE per-VM thing the golden image can't bake: the on-disk identity.
RENAME model (Contract A) — the baked `site.local` is renamed to the per-VM FQDN,
so the on-disk site name == the proxy Host header == the Site key. The production
gunicorn is multitenant (no `--site`), resolving the site from the request Host
per request, so the rename + a regenerated `server_name <fqdn>` vhost serve it
with NO restart. The owner is handed the SHARED baked Administrator password
(rotated after first login), so the deploy does NO `set-admin-password` — dropping
that ~28s CPU-throttled `bench frappe` boot is the main latency win.

Two functions, two execution sites (spec/14-self-serve.md "What runs where"):

- `deploy_site` drives `bench/deploy-site.py` IN THE GUEST over guest-SSH: it
  renames the baked site to the FQDN, regenerates the bench's nginx vhost
  (`bench setup nginx` + a v6 listener) and reloads — no password reset, no
  restart. The db root password is baked + shared; the admin password is the baked
  throwaway the owner rotates.
- `wait_for_http` runs ON THE CONTROLLER, polling the guest's public /128 :80
  until an HTTP 200 — the readiness signal that, and ONLY that, flips a Site to
  Running (Contract B). NOT the VM's `status == Running` (that means "jailer
  launched the microVM", not "Frappe is serving").
"""

import http.client
import shlex
import time
from pathlib import Path

import frappe

from atlas.atlas._ssh.transport import run_scp, run_ssh, ssh_key_file, wait_for_ssh
from atlas.atlas.proxy import _record_guest_task, _remote_parent
from atlas.atlas.ssh import connection_for_guest

# The committed deploy script ships in the repo's top-level `bench/` dir beside
# build.sh. A site VM is a CLONE of the golden snapshot, taken AFTER build.sh's
# uploads to /tmp were gone — so the deploy script is uploaded fresh per deploy,
# not assumed present. `..` resolves the app symlink to the repo root.
REMOTE_DEPLOY_DIRECTORY = "/tmp/atlas-deploy-site"
DEPLOY_SCRIPT_NAME = "deploy-site.py"

# The result line the in-guest script prints (mirrors scripts/lib/atlas/_task.py's
# contract, inlined in the guest script because the guest has no Atlas package).
RESULT_MARKER = "ATLAS_RESULT="

# Readiness probe (Contract B). `/api/method/ping` is Frappe's built-in
# unauthenticated whitelisted method: it returns 200 `{"message":"pong"}` once the
# web server is up AND the site DB resolves for the Host header — an honest "Frappe
# is serving THIS site" signal that does NOT depend on the setup-wizard state (the
# wizard only gates `/`, not the API). Probed for the FQDN Host header (Contract A)
# so multitenant routing is exercised, not just "some site answers".
READINESS_PATH = "/api/method/ping"
READINESS_TIMEOUT_SECONDS = 600
# Initial poll, then geometric backoff to READINESS_MAX_POLL_SECONDS. A warm clone
# is already serving and answers the first probe (or within a second of the web
# restart settling), so a tight initial poll shaves up to ~5s of pure granularity
# off a path the user is actively watching; the backoff keeps the rare slow cold
# bring-up from hammering the guest.
READINESS_POLL_SECONDS = 0.25
READINESS_MAX_POLL_SECONDS = 2.0


def _deploy_script_path() -> Path:
	return Path(frappe.get_app_path("atlas", "..")).resolve() / "bench" / DEPLOY_SCRIPT_NAME


def deploy_site(virtual_machine: str, site_name: str) -> None:
	"""Deploy one Frappe site into the (already booted) golden bench VM.

	Uploads `bench/deploy-site.py` to the guest and runs it as root over guest-SSH
	(the same path build_bench/build_proxy use): it renames the baked `site.local`
	dir to the FQDN (Contract A — the on-disk name now equals the proxy Host header
	and the Site key), regenerates the bench's nginx vhost (`server_name <fqdn>` + a
	v6 listener) and reloads, then confirms the bench serves on :80 (the port the
	edge proxy's south hop dials). The production gunicorn is multitenant (no
	`--site`), so it resolves the site from the request Host header per request — the
	rename + reload take effect with NO restart. The owner is handed the SHARED baked
	Administrator password (rotated after first login), so the deploy does NO
	`set-admin-password` — dropping that ~28s CPU-throttled `bench frappe` boot is the
	main latency win. A cold clone additionally runs `setup production` first.

	Recorded as a `deploy-site` Task row for the operator's audit trail, like every
	guest op. Fails loud (raises) on a non-zero exit so the Site is marked Failed."""
	import time

	def _trace(message: str, since: float | None = None) -> None:
		"""Sub-stage trace for the deploy, printed to the job log so the operator can
		see the SSH-wait vs in-guest-run split live (the deploy is the longest, most
		opaque stage of auto_provision). `since` adds the elapsed seconds."""
		suffix = f" ({time.monotonic() - since:.1f}s)" if since is not None else ""
		print(f"[deploy_site {site_name}] {message}{suffix}", flush=True)

	vm = frappe.get_doc("Virtual Machine", virtual_machine)
	connection = connection_for_guest(vm)
	local_script = str(_deploy_script_path())
	remote_script = f"{REMOTE_DEPLOY_DIRECTORY}/{DEPLOY_SCRIPT_NAME}"

	# Wait for the guest's sshd to actually answer BEFORE the first scp. The Site
	# orchestration only waits for VM *status* Running ("the microVM launched"), not
	# for sshd to be serving. A site VM is a CLONE of the golden snapshot, which
	# boots into a load storm — MariaDB/Redis/supervisor from the baked bench all
	# auto-start and the thin-snapshot CoW thrashes — so for the first ~minute sshd's
	# TCP port is open but the banner exchange times out under load. Going straight to
	# scp (as this did) fails the whole deploy on that transient. wait_for_ssh polls
	# the handshake until it succeeds (and forget_host's the address first, so a
	# recycled /128 with a changed host key re-pins instead of hard-failing
	# accept-new — the same trap build_proxy/build_bench guard). Mirrors the bench
	# bake + proxy build, which never hit this only because they reach a freshly
	# image-provisioned (light-boot) VM, not a service-heavy clone.
	_trace("waiting for guest sshd to answer …")
	_t = time.monotonic()
	wait_for_ssh(connection, timeout_seconds=300)
	_trace("sshd up; uploading deploy-site.py", since=_t)

	with ssh_key_file(connection.ssh_private_key) as key_path:
		run_ssh(
			connection,
			key_path,
			"mkdir -p " + shlex.quote(_remote_parent(remote_script)),
			timeout_seconds=60,
		)
		run_scp(connection, key_path, local_script, remote_script, timeout_seconds=300)
		# python3 explicitly: an SSH `command` is non-interactive and the script's
		# shebang is enough, but the deploy script needs the system python (it shells
		# out to the baked bench-cli, which owns its own uv venv). Warm: rename +
		# `setup nginx` + reload + probe (fast — no restart). Cold: also `setup
		# production` (nginx + supervisor) first, which takes longer.
		command = f"python3 {shlex.quote(remote_script)} --site-name {shlex.quote(site_name)}"
		# A warm-restored clone (resumed from a golden memory snapshot, not
		# booted): the deploy gates on the in-guest identity freshen having
		# completed for THIS VM before it renames the site — see deploy-site.py's
		# --warm-vm-uuid.
		if vm.warm_snapshot:
			command += f" --warm-vm-uuid {shlex.quote(vm.name)}"
		_trace(f"running deploy-site.py in guest ({'warm' if vm.warm_snapshot else 'cold'} path) …")
		_t = time.monotonic()
		stdout, stderr, code = run_ssh(connection, key_path, command, timeout_seconds=1800)
		_trace(f"in-guest deploy-site.py returned (exit {code})", since=_t)
	_record_guest_task(virtual_machine, "deploy-site", {"site": site_name}, stdout, stderr, code)
	if code != 0:
		frappe.throw(f"Deploy of {site_name} on {virtual_machine} failed (exit {code}): {stderr[-500:]}")


def wait_for_http(
	ipv6_address: str,
	host_header: str,
	*,
	port: int = 80,
	path: str = READINESS_PATH,
	timeout_seconds: int = READINESS_TIMEOUT_SECONDS,
	poll_seconds: float = READINESS_POLL_SECONDS,
	max_poll_seconds: float = READINESS_MAX_POLL_SECONDS,
) -> None:
	"""Block until the guest answers HTTP 200 on :80 — the readiness gate that,
	and only that, flips a Site to Running (Contract B). Mirrors `wait_for_ssh`'s
	structure (deadline = monotonic()+timeout; loop; sleep; raise on deadline).

	The signal is an HTTP 200 from the guest `:80`, NOT the VM's `status ==
	Running` — that distinction IS Contract B; do not "optimize" it back to the VM
	status. We probe over the VM's public /128 (the v6 literal goes in brackets in
	the host arg — the `scp v6 needs brackets` trap applies to any v6 URL host),
	with the FQDN Host header (Contract A) — the same Host the edge proxy forwards.
	The bench's nginx serves it via its default_server block (the on-disk site is
	`site.local`, served for any Host), so this exercises the real south-hop path.
	The controller is off-host, so this is an honest end-to-end probe over the same
	path the proxy uses — not a host-local shortcut.

	The poll starts at `poll_seconds` and backs off geometrically to
	`max_poll_seconds`: a warm clone answers the first probe, so the tight start
	removes the old flat-5s granularity from the readiness wait. Raises
	frappe.ValidationError on timeout."""
	deadline = time.monotonic() + timeout_seconds
	poll = poll_seconds
	while True:
		if _http_ok(ipv6_address, host_header, port, path):
			return
		if time.monotonic() >= deadline:
			raise frappe.ValidationError(
				f"HTTP 200 from {host_header} ([{ipv6_address}]:{port}{path}) not seen after {timeout_seconds}s"
			)
		time.sleep(poll)
		poll = min(poll * 1.5, max_poll_seconds)


def _http_ok(ipv6_address: str, host_header: str, port: int, path: str) -> bool:
	"""One probe: GET path over IPv6 with the FQDN Host header; True iff 200.

	A pre-serving guest refuses or resets the connection (nginx not up yet) or 502s
	(nginx up, site/supervisor not) — every such transport/HTTP error is a normal
	'not ready yet', swallowed so the poll loop keeps trying until the deadline.
	Only a clean 200 ends the wait. The guest is reached on its /128 over the public
	v6 internet (spec/06: no private fabric); `http.client.HTTPConnection` takes the
	bare v6 literal and `socket.create_connection` resolves it to AF_INET6."""
	conn = None
	try:
		conn = http.client.HTTPConnection(ipv6_address, port, timeout=10)
		conn.request("GET", path, headers={"Host": host_header})
		return conn.getresponse().status == 200
	except (OSError, http.client.HTTPException):
		return False
	finally:
		if conn is not None:
			conn.close()
