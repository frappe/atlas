#!/usr/bin/env python3
# Deploy ONE Frappe site into a golden bench VM — run INSIDE the guest over
# guest-SSH (spec/14-self-serve.md), driven by the controller
# `atlas.atlas.deploy_site.deploy_site`. The golden image (spec/08-images.md)
# already baked bench-cli + `bench init` AND a fully-created site under the fixed
# name `site.local`, brought up production-style and (warm) frozen serving. So the
# ONLY per-VM work the image can't bake is giving that baked site its per-VM
# identity — the FQDN — on disk.
#
# RENAME model (Contract A): the baked `site.local` is renamed to the per-VM FQDN
# at deploy time, so the on-disk site name == the proxy Host header == the Site
# key (one routing string, never transformed). The production gunicorn is
# MULTITENANT — `frappe.app:application` runs with no fixed `--site`, so it
# resolves the site from the request `Host` header PER REQUEST (frappe/app.py
# `get_site_name(request.host)`), with nothing cached at boot. The proxy forwards
# `Host: <fqdn>`, so once `sites/<fqdn>` exists on disk and the bench's nginx vhost
# carries `server_name <fqdn>`, the running gunicorn serves it with NO restart.
# The deploy is `bench new-site`-free (baked) and `set-admin-password`-free — the
# baked Administrator password is a long random secret generated at bake time and
# never surfaced. Instead, site mode mints a one-click session URL with
# `bench browse --user Administrator` (a real 24h session, no password);
# admin mode mints one with `bench generate-admin-session --full-path` (Pilot #117,
# a 5-minute single-use JWT). Either way the result carries `login_url` — the only
# way in besides a password the tenant/operator sets themselves later.
#
# The rename is one bench-cli command: `bench rename-site <old> <new>`
# (bench-setup-manual.md) moves the site dir, updates the site config, regenerates
# nginx (`server_name <fqdn>`, `root .../sites/<fqdn>/public`, on both `listen 80;`
# and `listen [::]:80;` — bench-cli emits the v6 listener itself, the edge proxy
# reaches the VM over its public /128 only), and re-runs production setup for the
# new domain. Production setup is idempotent, so it is a fast no-op on a clone that
# was baked production-style — no full rebuild, no per-clone nginx surgery here.
#
# This ships in the committed `bench/` tree (beside build.sh), uploaded verbatim
# and run over guest-SSH — the same idiom as build.sh. It is self-contained
# (stdlib only, no host `scripts/lib`): the guest never has the Atlas package, so
# the typed-task shape (kebab-case flags via argparse, one `ATLAS_RESULT={json}`
# line out) is inlined here rather than imported.
#
# Run as root with the baked PATH (build.sh wrote /etc/profile.d/atlas-bench.sh,
# but an SSH `command` is non-interactive and does NOT source profile.d, so the
# controller invokes us with an explicit interpreter + the bench-cli path passed
# in). Idempotent (spec taste #14: retry = re-run): a re-run finds `sites/<fqdn>`
# already in place (the baked `site.local` is gone) and just re-asserts the vhost
# + serving.

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass

RESULT_MARKER = "ATLAS_RESULT="

# Where build.sh installed bench-cli and the baked bench. Kept in lockstep with
# bench/build.sh (BENCH_USER / BENCH_CLI_DIR / BENCH_NAME) — if the bake moves
# them, move them here too. The golden is baked AS the unprivileged `frappe` user
# (its lingering systemd --user units are what make a clone boot serving), so the
# bench lives under that user's home and every bench command runs as `frappe`.
BENCH_USER = "frappe"
BENCH_HOME = f"/home/{BENCH_USER}"
# ~/pilot since the frappe/bench-cli → frappe/pilot rename (install.sh fc89e51+
# clones there); see bench/build.sh's BENCH_CLI_DIR note.
BENCH_CLI_DIR = f"{BENCH_HOME}/pilot"
BENCH_NAME = "atlas"
BENCH_DIR = f"{BENCH_CLI_DIR}/benches/{BENCH_NAME}"
BENCH = f"{BENCH_CLI_DIR}/bench"

# The site baked into the golden image (bench/build.sh BAKED_SITE, site mode). The
# per-site deploy renames this directory to the FQDN; a clone that doesn't carry it
# was built from the wrong/old (site-less) snapshot — _preflight fails loud on that.
BAKED_SITE = "site.local"
SITES_DIR = f"{BENCH_DIR}/sites"
# The committed bench.toml on the golden — admin mode rewrites its `[admin].domain`
# to the FQDN before `bench setup production` regenerates the admin vhost.
BENCH_TOML = f"{BENCH_DIR}/bench.toml"


@dataclass(frozen=True)
class DeploySiteInputs:
	"""Per-site deploy inputs. `site_name` is the full FQDN (Contract A) — the
	on-disk site dir name after the rename, the proxy Host header, and the Site
	key, one string never transformed. `warm_vm_uuid` is set when this VM was
	warm-restored from a golden memory snapshot: the deploy then asserts the
	in-guest identity freshen completed for exactly this VM before it renames.

	`mode` picks what the FQDN maps to (mirrors build.sh's bake mode):
	  * site  — `bench rename-site` the baked `site.local` dir to the FQDN, so the
	            FQDN serves the baked site (rename + nginx + production setup in one).
	  * admin — set `[admin].domain = <fqdn>` then `bench setup production` so the
	            FQDN serves the admin app (no site rename; the golden carries no baked
	            site in admin mode).
	Either way bench-cli regenerates nginx to map it correctly (it emits the v6
	listeners itself at the pinned commit).

	There is no per-VM admin password here: the baked throwaway Administrator
	password ships with the golden and is rotated out of band, NOT reset on the
	signup path (resetting it cost a full CPU-throttled `bench frappe` boot —
	~28s under the 0.25-core cap — which dominated the deploy)."""

	site_name: str
	warm_vm_uuid: str = ""
	mode: str = "site"
	central_endpoint: str = ""
	central_auth_token: str = ""

	@classmethod
	def from_args(cls, argv: list[str] | None = None) -> "DeploySiteInputs":
		parser = argparse.ArgumentParser(prog="deploy-site", description=cls.__doc__)
		parser.add_argument("--site-name", required=True, help="Full FQDN, e.g. acme.blr1.frappe.dev")
		parser.add_argument(
			"--warm-vm-uuid",
			default="",
			help="This VM's uuid when it was warm-restored; gates on the in-guest freshen",
		)
		parser.add_argument(
			"--mode",
			choices=("site", "admin"),
			default="site",
			help="site: map the FQDN to the baked site (rename). admin: map it to the admin app",
		)
		parser.add_argument(
			"--central-endpoint", default="", help="Central API base URL the pilot calls back on"
		)
		parser.add_argument(
			"--central-auth-token", default="", help="Opaque token the pilot presents to Central"
		)
		ns = parser.parse_args(argv)
		return cls(
			site_name=ns.site_name,
			warm_vm_uuid=ns.warm_vm_uuid,
			mode=ns.mode,
			central_endpoint=ns.central_endpoint,
			central_auth_token=ns.central_auth_token,
		)


@dataclass(frozen=True)
class DeploySiteResult:
	"""What the deploy records on the Task row for the operator's audit trail. `site`
	is the FQDN the deploy served; `serving` is the in-guest local probe's verdict;
	`login_url` is the one-click handoff URL, replacing a shared password either
	way: site mode mints it with `bench browse` (a real 24h session, built
	into `https://<fqdn>/app?sid=<sid>` — Contract A: the FQDN is the one routing
	string, HTTPS terminates at the edge proxy, never in-guest); admin mode mints it
	with `bench generate-admin-session --full-path` (a 5-minute single-use JWT,
	Pilot #117)."""

	site: str
	serving: bool
	login_url: str = ""

	def emit(self) -> None:
		payload = {"site": self.site, "serving": self.serving}
		if self.login_url:
			payload["login_url"] = self.login_url
		print(RESULT_MARKER + json.dumps(payload))


def _run(args: list[str], *, capture: bool = False) -> str:
	"""Run a command, streaming to our stdout/stderr (so the controller's Task row
	captures the bench output) unless `capture`, in which case return stdout. Fail
	loud: a non-zero exit aborts the deploy (the controller marks the Site Failed)."""
	env = dict(os.environ)
	env["DEBIAN_FRONTEND"] = "noninteractive"
	try:
		result = subprocess.run(
			args,
			env=env,
			text=True,
			capture_output=capture,
			check=True,
		)
	except subprocess.CalledProcessError as e:
		# When we capture output, the failing command's own stdout/stderr is on the
		# exception, not the Task log — surface it before re-raising so the real
		# error (e.g. why `bench browse` exited non-zero) isn't swallowed.
		if capture:
			if e.stdout:
				print(e.stdout, end="")
			if e.stderr:
				print(e.stderr, end="", file=sys.stderr)
		raise
	return result.stdout if capture else ""


def _bench(*args: str, capture: bool = False) -> str:
	"""Invoke the baked bench-cli against the baked bench (`bench -b atlas …`) AS the
	frappe user, through a login shell so the PATH install.sh wrote into ~/.bashrc
	(bench-cli + uv) resolves — the same way the bake reached `bench`. The controller
	runs this script as root over SSH, so we drop to `frappe` with `sudo -u`."""
	inner = "cd " + shlex.quote(BENCH_CLI_DIR) + " && " + shlex.join([BENCH, "-b", BENCH_NAME, *args])
	return _run(["sudo", "-u", BENCH_USER, "bash", "-lc", inner], capture=capture)


def _await_freshen(warm_vm_uuid: str, timeout_seconds: int = 60) -> None:
	"""Gate a warm deploy on the in-guest identity freshen having completed for
	THIS VM. Reaching the guest over its own /128 already implies the network
	half happened (the freshen brings the clone's addresses up last), so the
	marker is normally present on the first read — the wait covers the
	marker-write race, the timeout the pathological 'reached over a stale path'
	case. Fail loud: deploying a site onto a clone that still carries the
	golden's identity must never proceed."""
	import time

	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		try:
			# nosemgrep: frappe-security-file-traversal -- guest script; reads the fixed /etc/atlas-vm-uuid path, not untrusted web input
			with open("/etc/atlas-vm-uuid") as handle:
				if handle.read().strip() == warm_vm_uuid:
					return
		except OSError:
			pass
		# Short poll: the marker is normally present on the very first read (the
		# freshen brings the network up LAST, so reaching the guest implies it's
		# done), so a tight interval just shaves the marker-write race off a path
		# the user is actively watching — not a busy-wait in practice.
		time.sleep(0.2)
	sys.exit(
		f"warm freshen did not complete for {warm_vm_uuid} within {timeout_seconds}s; "
		"this clone still carries the golden's identity"
	)


def _await_db_ready(timeout_seconds: int = 60) -> None:
	"""Gate the deploy on the baked bench's MariaDB instance actually accepting
	connections before any DB-touching step (rename-site / browse / setup).

	The instance is a system `mariadb@<bench>.service` (Type=notify, so `active`
	means it has opened its socket). It is ordered only `After=network.target`
	with NO ordering against sshd — so on a snapshot-booted clone sshd can (and
	does) win the race and answer while MariaDB is still in its ~15s startup.
	The controller then connects and runs the deploy before the socket exists;
	`rename-site` survives (its production-setup brings the DB up / retries), but
	`bench browse` connects with a bare `frappe.connect()` and no retry, so it
	dies with `(2002) Can't connect ... mysqld-<bench>.sock`. Waiting for the
	unit to report active closes that window. Fail loud on timeout — a deploy
	onto a bench whose DB never came up cannot mint a session."""
	unit = f"mariadb@{BENCH_NAME}.service"
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		probe = subprocess.run(
			["systemctl", "is-active", "--quiet", unit],
		)
		if probe.returncode == 0:
			return
		time.sleep(0.5)
	sys.exit(f"{unit} did not become active within {timeout_seconds}s; the bench DB is not up")


def _preflight() -> None:
	"""Assert this is a golden bench VM, not plain Ubuntu. A missing bench-cli or
	baked bench means the VM was cloned from the wrong/old snapshot — fail loud
	before any per-VM work (the wrong image is unrecoverable here, not retryable).
	The site-vs-admin baked-content check is mode-specific and lives in the rename
	path (`_rename_site_to_fqdn`) / admin path, not here."""
	if not os.path.exists(BENCH):
		sys.exit(f"bench-cli not found at {BENCH}; this VM was not baked from the golden image")
	if not os.path.isdir(BENCH_DIR):
		sys.exit(f"baked bench {BENCH_DIR} missing; this VM was not baked from the golden image")


def _rename_site_to_fqdn(fqdn: str) -> bool:
	"""Rename the baked `sites/site.local` to `<fqdn>` via `bench rename-site` — the
	one piece of per-VM on-disk identity (Contract A). Returns True if it renamed,
	False if the rename was already done (a re-run / idempotency).

	`bench rename-site <old> <new>` (bench-setup-manual.md) is the first-class
	bench-cli command: it moves the site dir, updates the site config, regenerates
	nginx, and re-runs production setup for the new domain — replacing the old
	hand-rolled `os.rename` + separate `bench setup nginx`. The production gunicorn
	is multitenant (resolves the site from the `Host` header per request), so once
	`sites/<fqdn>` exists and the vhost says `server_name <fqdn>` the workers serve
	it without a restart.

	Fails loud if neither the baked dir nor an already-renamed `<fqdn>` dir exists:
	the clone came from a site-less snapshot and can never serve."""
	baked = os.path.join(SITES_DIR, BAKED_SITE)
	target = os.path.join(SITES_DIR, fqdn)
	if os.path.isdir(target):
		# Already renamed (idempotent re-run). The baked dir must be gone too — if
		# both exist something is wrong, but the FQDN dir is what serves, so proceed.
		return False
	if not os.path.isdir(baked):
		sys.exit(
			f"neither baked site {baked} nor renamed {target} exists; this VM was cloned "
			f"from a site-less snapshot, not the baked-site golden image — re-bake or fix "
			f"default_bench_snapshot"
		)
	_bench("rename-site", BAKED_SITE, fqdn)
	return True


def _mint_login_url(fqdn: str) -> str:
	"""Site mode only: mint a real 24h Administrator session and return the
	one-click login URL — the tenant handoff, replacing a shared password.

	`bench browse --user Administrator` (stock Frappe) logs in as Administrator
	(the one user `browse` allows without developer_mode) and prints
	`Login URL: <url>?sid=<sid>` before its trailing `click.launch(url)`. There is
	no `--sid` flag (verified against this bench's checked-out
	frappe/commands/site.py — `browse` only takes `--user`/`--session-end`/
	`--user-for-audit`), so the sid is pulled out of the printed URL instead.
	`click.launch` is harmless here: on Linux it Popens `xdg-open` without
	waiting, so it returns immediately even when nothing is installed to handle
	it — it does not block or hang this headless guest. `--session-end` pins the
	session to a fixed 24h from now, ISO8601 UTC, matching Pilot's post-exchange
	admin cookie TTL. No `set-admin-password` anywhere on this path — the baked
	password (randomized at bake time) is never touched."""
	import datetime
	import re

	session_end = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=24)).isoformat()
	output = _bench(
		"frappe",
		"--site",
		fqdn,
		"browse",
		"--user",
		"Administrator",
		"--session-end",
		session_end,
		capture=True,
	)
	match = re.search(r"sid=(\S+)", output)
	if not match:
		sys.exit(f"bench browse did not print a Login URL with a sid: {output!r}")
	return f"https://{fqdn}/app?sid={match.group(1)}"


def _mint_admin_login_url() -> str:
	"""Admin mode only: mint the admin console's one-click sign-in URL, replacing
	the shared baked `[admin].password` handoff.

	`bench generate-admin-session --full-path` (Pilot #117, bench-cli) issues a
	5-minute single-use `?sid=` JWT, signed by `admin.jwt_secret` (auto-generated
	in bench.toml on first call) — the admin frontend exchanges it for a 1-day
	HttpOnly session cookie. Password login still works but is no longer the
	handoff. Run AFTER `_set_admin_domain` so the printed URL already carries the
	real FQDN, not the placeholder `admin.localhost`."""
	return _bench("generate-admin-session", "--full-path", capture=True).strip()


def _set_admin_domain(fqdn: str) -> None:
	"""Admin mode: point the admin vhost at the FQDN, then run production setup.

	With `[admin].domain = <fqdn>` set, `bench setup production` emits an
	`_admin.conf` vhost (`server_name <fqdn>`, `listen 80;` + `listen [::]:80;`)
	proxying to the socket-activated admin gunicorn — so the FQDN maps to the admin
	URL. We rewrite the committed bench.toml's `domain = ""` line in place (a plain
	text edit — no TOML library in the guest, stdlib-only) then run
	`bench setup production`. Idempotent: re-running rewrites the same line and
	production setup is a fast no-op when already done. Fails loud if the admin
	domain line is absent (a clone from the wrong/old snapshot)."""
	# nosemgrep: frappe-security-file-traversal -- guest script; reads the fixed BENCH_TOML path, not untrusted web input
	with open(BENCH_TOML) as f:
		text = f.read()
	out_lines = []
	replaced = False
	for line in text.splitlines(keepends=True):
		if line.lstrip().startswith("domain") and "=" in line and not replaced:
			indent = line[: len(line) - len(line.lstrip())]
			out_lines.append(f'{indent}domain = "{fqdn}"\n')
			replaced = True
		else:
			out_lines.append(line)
	if not replaced:
		sys.exit(f"no [admin].domain line in {BENCH_TOML}; this VM was not baked from an admin-mode golden")
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed BENCH_TOML path, not untrusted web input
	with open(BENCH_TOML, "w") as f:
		f.write("".join(out_lines))
	_bench("setup", "production")


# The local readiness path, per bake mode. site mode serves a Frappe site whose
# built-in unauthenticated `/api/method/ping` returns 200; admin mode serves the
# bench-cli admin console — a FLASK app with NO `/api/method/ping` (it would 404),
# whose unauthenticated health endpoint is `/api/status` (admin/backend app.py
# `_OPEN_PATHS`). Kept in lockstep with the controller's deploy_site.READINESS_PATH /
# readiness_path_for_mode.
_HEALTH_PATH = {"site": "/api/method/ping", "admin": "/api/status"}


def _serving(host_header: str, mode: str) -> bool:
	"""Best-effort in-guest confirmation that the front door answers locally before
	we report serving. The controller's wait_for_http is the authoritative gate
	(Contract B, end-to-end over the real network); this is a fast local sanity
	check so a deploy that silently failed to bring nginx up surfaces here too.

	Probe over **IPv6** (`[::1]`) AND v4 — the edge proxy reaches the VM over its
	public /128, so a v6 200 proves the path that matters is wired. The Host header
	is the FQDN (Contract A); in site mode the multitenant gunicorn resolves the
	renamed site from it, in admin mode nginx routes it to the admin app. The health
	PATH is mode-aware (the admin app has no Frappe ping route)."""
	path = _HEALTH_PATH.get(mode, _HEALTH_PATH["site"])
	return _local_ping(host_header, "[::1]", path) and _local_ping(host_header, "127.0.0.1", path)


def _local_ping(site_name: str, host_ip: str, path: str) -> bool:
	try:
		out = subprocess.run(
			[
				"curl",
				"-s",
				"-g",
				"-o",
				"/dev/null",
				"-w",
				"%{http_code}",
				"-H",
				f"Host: {site_name}",
				f"http://{host_ip}:80{path}",
			],
			text=True,
			capture_output=True,
			timeout=30,
			check=False,
		)
		return out.stdout.strip() == "200"
	except Exception:
		return False


def _stage_logger():
	"""A tiny in-guest stage tracer: returns a `log(msg)` that prints `[deploy-site
	+Ns]` to stdout (captured on the controller's Task row + streamed to the job
	log), so the operator following auto_provision sees which in-guest step is slow.
	Stdlib-only, like the rest of this script."""
	import time

	t0 = time.monotonic()

	def log(message: str) -> None:
		print(f"[deploy-site +{time.monotonic() - t0:5.1f}s] {message}", flush=True)

	return log


def main() -> None:
	"""Deploy one FQDN into a golden bench VM — site mode (RENAME) or admin mode.

	site mode: the baked `site.local` is renamed to the per-VM FQDN, so the on-disk
	site name == the proxy Host header == the Site key (Contract A). The production
	gunicorn is multitenant (no `--site`), resolving the site from the request `Host`
	per request, so the rename + the regenerated `server_name <fqdn>` vhost take
	effect with NO restart.

	admin mode: no site is baked; instead `[admin].domain` is set to the FQDN so the
	regenerated nginx admin vhost routes the FQDN to the socket-activated admin app.

	The deploy is: (warm) gate on the identity freshen → (cold) ensure `bench start`
	→ map the FQDN — site mode `bench rename-site`s the baked dir to the FQDN (rename
	+ nginx + production setup in one); admin mode sets [admin].domain then
	`bench setup production`. Production setup is idempotent, so a clone baked
	production-style re-runs it as a fast no-op (v6 listener included by bench-cli) →
	local serving probe. No `set-admin-password` (the baked throwaway is rotated out
	of band). Every bench command runs as the `frappe` user (the bake user).

	A warm clone (resumed from a memory snapshot) is already serving; a cold clone
	(snapshot-booted) idempotently re-asserts `bench start` first."""
	inputs = DeploySiteInputs.from_args()
	log = _stage_logger()
	log(
		f"deploy start (fqdn={inputs.site_name}, baked={BAKED_SITE}, "
		f"{'warm' if inputs.warm_vm_uuid else 'cold'})"
	)
	_preflight()
	if inputs.warm_vm_uuid:
		log("awaiting warm identity freshen marker …")
		_await_freshen(inputs.warm_vm_uuid)
		log("freshen complete")

	# COLD only: ensure the production stack is up. The golden was baked with
	# `bench start` (its lingering systemd --user units enabled), so a snapshot-
	# booted clone normally comes up serving on its own; this is an idempotent
	# belt-and-suspenders `bench start` (a no-op if the target is already active)
	# for the cold fallback. A warm clone is already serving and skips it.
	if not inputs.warm_vm_uuid:
		log("cold: ensuring bench is started …")
		_bench("start")
		log("cold bring-up done")

	# The DB instance races sshd on a snapshot-booted clone (see _await_db_ready):
	# gate here, once the stack is up, so neither rename-site nor the session mint
	# connects before MariaDB has opened its socket.
	log("waiting for the bench DB to accept connections …")
	_await_db_ready()
	log("bench DB ready")

	# The per-VM front door: map the FQDN to the baked site (rename) or the admin
	# app (set [admin].domain), then regenerate the nginx vhost + reload — no
	# gunicorn/supervisor restart. bench-cli emits the v6 listener itself.
	login_url = ""
	if inputs.mode == "admin":
		log("admin mode: pointing [admin].domain at the FQDN + setup production …")
		_set_admin_domain(inputs.site_name)
		log("admin vhost regenerated + reloaded")
		log("minting admin login URL (bench generate-admin-session --full-path) …")
		login_url = _mint_admin_login_url()
		log("admin login URL minted")
	else:
		# `bench rename-site` moves the site, regenerates nginx, AND re-runs
		# production setup for the new domain in one step — so there is no separate
		# `bench setup nginx` here anymore. It is fast on a re-run / already-renamed
		# clone (production setup is idempotent).
		log("renaming baked site to the FQDN (bench rename-site) …")
		renamed = _rename_site_to_fqdn(inputs.site_name)
		log(f"rename {'done' if renamed else 'already in place'}")
		log("minting tenant login URL (bench browse) …")
		login_url = _mint_login_url(inputs.site_name)
		log("login URL minted")

	# Central handoff: persist the pilot's callback endpoint + token into the bench's
	# bench.toml (Pilot owns that file, so we go through its command rather than writing
	# TOML here), so pilot→Central calls can authenticate with X-Pilot-Token.
	if inputs.central_endpoint and inputs.central_auth_token:
		log("writing Central config to bench.toml (bench set-central-config) …")
		_bench(
			"set-central-config", "--endpoint", inputs.central_endpoint, "--token", inputs.central_auth_token
		)
		log("Central config written")

	log("local serving probe (v6 + v4) …")
	serving = _serving(inputs.site_name, inputs.mode)
	log(f"deploy complete (serving={serving})")
	result = DeploySiteResult(site=inputs.site_name, serving=serving, login_url=login_url)
	result.emit()


if __name__ == "__main__":
	main()
