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
# admin mode mints one with the bench-cli admin session verb (a 5-minute single-use
# JWT), when the golden's bench-cli still carries one — `login_url` is then the only
# way in besides a password the tenant/operator sets themselves later. A golden whose
# bench-cli dropped that verb emits NO `login_url` and the console falls back to its
# baked `[admin].password`; see `_mint_admin_login_url`.
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
import socket
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
# The bench DB's UNIX socket — the readiness contract `_await_db_ready` probes, and
# what the baked site_config.json's `db_socket` points at. pilot v0.0.9 runs MariaDB
# as a user-owned `pilot-mariadb.service` with its datadir + socket under the pilot
# dir; the older fork ran a system `mariadb@<bench>` instance instead. We probe the
# socket precisely so the deploy doesn't care which. See `_await_db_ready`.
DB_SOCKET = f"{BENCH_CLI_DIR}/databases/mariadb/run/mysqld.sock"

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
	admin_domain: str = ""
	central_endpoint: str = ""
	bootstrap_token: str = ""
	regenerate_login: bool = False

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
			"--admin-domain",
			default="",
			help=(
				"FQDN to write into `[admin].domain` regardless of mode — the admin console's "
				"host. Site mode: the attached Pilot's FQDN, set BEFORE the rename so rename-site's "
				"production setup emits the admin vhost in the same pass. Admin mode: normally the "
				"same as --site-name; when omitted admin mode falls back to --site-name."
			),
		)
		parser.add_argument(
			"--central-endpoint", default="", help="Central API base URL the pilot calls back on"
		)
		parser.add_argument(
			"--bootstrap-token", default="", help="Single-use enrollment token the pilot exchanges at Central"
		)
		parser.add_argument(
			"--regenerate-login",
			action="store_true",
			help=(
				"Re-mint the one-click login URL ONLY — the site is already deployed "
				"(renamed / admin domain set), so skip the rename + production setup + "
				"Central config and just print a fresh ATLAS_RESULT with a new login_url. "
				"Drives the short-lived-token refresh Central asks for on a late click."
			),
		)
		ns = parser.parse_args(argv)
		return cls(
			site_name=ns.site_name,
			warm_vm_uuid=ns.warm_vm_uuid,
			mode=ns.mode,
			admin_domain=ns.admin_domain,
			central_endpoint=ns.central_endpoint,
			bootstrap_token=ns.bootstrap_token,
			regenerate_login=ns.regenerate_login,
		)


@dataclass(frozen=True)
class DeploySiteResult:
	"""What the deploy records on the Task row for the operator's audit trail. `site`
	is the FQDN the deploy served; `serving` is the in-guest local probe's verdict;
	`login_url` is the one-click handoff URL, replacing a shared password either
	way: site mode mints it with `bench browse` (a real 24h session, built
	into `https://<fqdn>/app?sid=<sid>` — Contract A: the FQDN is the one routing
	string, HTTPS terminates at the edge proxy, never in-guest); admin mode mints it
	with the bench-cli admin session verb (a 5-minute single-use JWT). It is OMITTED
	from the payload when empty — an admin console on a bench-cli with no session
	verb still deploys and still serves, it just has no one-click link
	(`_mint_admin_login_url`), so every consumer must treat it as optional."""

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

	The DB is ordered only `After=network.target` with NO ordering against sshd —
	so on a snapshot-booted clone sshd can (and does) win the race and answer while
	MariaDB is still in its ~15s startup. The controller then connects and runs the
	deploy before the socket exists; `rename-site` survives (its production-setup
	brings the DB up / retries), but `bench browse` connects with a bare
	`frappe.connect()` and no retry, so it dies with `(2002) Can't connect ...`.
	Waiting for the socket to accept closes that window.

	We probe the SOCKET, not a unit name, because the socket is the stable contract
	and the unit behind it is not. Up to the previous fork the DB was a system
	`mariadb@<bench>.service` (multi-instance, own datadir on the ZFS pool);
	pilot v0.0.9-pre-alpha moved it to a user-owned `pilot-mariadb.service` under
	the bench user, datadir under the pilot dir, no ZFS at all. We run as root, so
	`systemctl is-active mariadb@atlas` could never succeed against a v0.0.9 golden
	and EVERY deploy failed here at the timeout. A socket probe is indifferent to
	which shape the golden carries — and to the next rename. Fail loud on timeout —
	a deploy onto a bench whose DB never came up cannot mint a session."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		if os.path.exists(DB_SOCKET):
			probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			try:
				probe.connect(DB_SOCKET)
				return
			except OSError:
				# Socket file is published but mariadbd isn't accepting yet.
				pass
			finally:
				probe.close()
		time.sleep(0.5)
	sys.exit(f"{DB_SOCKET} did not accept a connection within {timeout_seconds}s; the bench DB is not up")


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


# The spellings of the admin session-minting verb, newest first. UPSTREAM PILOT SHIPS
# NEITHER — its `bench admin` group is build, enroll, issue-site-token, revoke-totp,
# run-patches, set-central-config, upgrade — and does not need to: an enrolled bench
# trusts Central's JWKS, so CENTRAL mints the console's one-click `?sid=` link itself
# (central/api/sso.get_bench_link) and no in-guest verb is involved. These two are kept
# for older/forked goldens that do carry one (the top-level `generate-admin-session` of
# Pilot #117, and the grouped spelling a future regroup would use), tried newest-first
# so a release that restores the verb needs no change here. Finding none is the normal
# upstream case and DEGRADES, it does not fail (see `_mint_admin_login_url`).
_ADMIN_SESSION_VERBS = (("admin", "generate-session"), ("generate-admin-session",))


def _missing_verb(error: subprocess.CalledProcessError) -> bool:
	"""True iff a failed `_bench(...)` died because the VERB does not exist on this
	golden's bench-cli, rather than because the mint itself broke.

	TWO different parsers answer here and they word it differently — both must be
	matched, or the grouped spelling reads as a real failure and fails the deploy loud:

	* An unknown TOP-level verb falls through to the Frappe passthrough (click):
	  exit 2, `No such command` on stderr.
	* An unknown subcommand under a KNOWN group (`bench admin …`) never reaches click.
	  Pilot's group parser is argparse, which exits 2 with
	  `argument admin_command: invalid choice: 'generate-session' (choose from …)`.

	Matching only click's phrasing is what let a v0.0.9 golden — which has the `admin`
	group but no session verb in it — re-raise out of `_mint_admin_login_url`, mark the
	companion console Failed, and (because the Pilot creates its proxy route only after
	the mint) leave the console FQDN unrouted entirely.

	Exit 2 is still required alongside the phrase, so a REAL failure — an unwritable
	`admin.jwt_secret`, a wedged bench — still propagates and fails the deploy loud."""
	streams = f"{error.stdout or ''}\n{error.stderr or ''}".lower()
	if error.returncode != 2:
		return False
	return any(phrase in streams for phrase in ("no such command", "unknown command", "invalid choice"))


def _regrouped(group: str, verb: str, *args: str, capture: bool = False) -> str:
	"""Run a pilot verb that moved under a CLI GROUP, newest spelling first, falling
	back to the legacy top-level one.

	THIS SCRIPT RUNS ON ANY GOLDEN, WHATEVER PILOT IT CARRIES. `issue-site-token` and
	`enroll` are `bench admin <verb>` on current pilot, and were top-level before the
	regroup — and on a pre-regroup tree there is no `admin` group at all, so the grouped
	spelling is not a bad subcommand under a known group, it is an unrecognised LEADING
	verb, which pilot's dispatcher hands to Frappe as a passthrough. `_missing_verb`
	recognises both wordings, so one fallback covers both shapes: an image baked before
	the regroup (or off a fork) still deploys against this same script.

	NO degrade here, unlike `_mint_admin_login_url`: a verb we were asked to run and
	cannot find anywhere is a bake/pin bug, not a poorer handoff. Degrading would be
	worse than failing — an un-enrolled bench that reports success reaches `Running`
	and then refuses to open from Central with nothing to explain why, which is the
	exact failure mode 8f7a591 was written to remove. So we fail, but we fail
	ACTIONABLY: naming both spellings tried and where the pin lives, because click's
	bare `No such command` on a Frappe passthrough is the confusing part, not the exit
	code. A verb that exists and BROKE still propagates unchanged (`_missing_verb` is
	what tells the two apart)."""
	try:
		return _bench(group, verb, *args, capture=capture)
	except subprocess.CalledProcessError as error:
		if not _missing_verb(error):
			raise
	try:
		return _bench(verb, *args, capture=capture)
	except subprocess.CalledProcessError as error:
		if not _missing_verb(error):
			raise
		sys.exit(
			f"this golden's pilot carries `{verb}` at neither `bench {group} {verb}` nor "
			f"`bench {verb}` — it cannot do what the deploy was asked to do. Re-bake the image "
			f"(the recipe installs pilot the documented way, so a re-bake picks up the current "
			f"release; image_recipes._BENCH_CLI_REPO/_BENCH_CLI_REF pin it elsewhere), or stop "
			f"passing the input that requests it."
		)


def _mint_admin_login_url() -> str:
	"""Admin mode only: mint the admin console's one-click sign-in URL, replacing
	the shared baked `[admin].password` handoff. Returns "" when this golden's
	bench-cli carries no session-minting verb at all.

	The verb (`--full-path`, either spelling in `_ADMIN_SESSION_VERBS`) issues a
	5-minute single-use `?sid=` JWT, signed by `admin.jwt_secret` (auto-generated
	in bench.toml on first call) — the admin frontend exchanges it for a 1-day
	HttpOnly session cookie. Password login still works but is no longer the
	handoff. Run AFTER `_set_admin_domain` so the printed URL already carries the
	real FQDN, not the placeholder `admin.localhost`.

	DEGRADE, don't die, when neither spelling exists: the console is still fully
	reachable at its FQDN with the baked `[admin].password`, so an absent one-click
	link is a poorer handoff, not a failed deploy — and this same script deploys the
	tenant's SITE, which must not be marked Failed because its companion console
	could not mint a URL. A missing verb is detected precisely (`_missing_verb`);
	anything else re-raises and fails loud."""
	for verb in _ADMIN_SESSION_VERBS:
		try:
			return _bench(*verb, "--full-path", capture=True).strip()
		except subprocess.CalledProcessError as error:
			if not _missing_verb(error):
				raise
	print(
		"WARNING: this bench-cli carries no admin session verb ("
		+ " / ".join("bench " + " ".join(verb) for verb in _ADMIN_SESSION_VERBS)
		+ "); the admin console has no one-click login URL — sign in with [admin].password instead",
		file=sys.stderr,
		flush=True,
	)
	return ""


def _set_admin_domain(fqdn: str, *, run_setup: bool = True, update_site: str = "") -> None:
	"""Point the admin vhost at the FQDN by rewriting `[admin].domain`, and (unless
	`run_setup` is False) run production setup so the vhost regenerates.

	With `[admin].domain = <fqdn>` set, `bench setup production` emits an
	`_admin.conf` vhost (`server_name <fqdn>`, `listen 80;` + `listen [::]:80;`)
	proxying to the socket-activated admin gunicorn — so the FQDN maps to the admin
	URL. We rewrite the committed bench.toml's `domain = ""` line in place (a plain
	text edit — no TOML library in the guest, stdlib-only). Idempotent: re-running
	rewrites the same line and production setup is a fast no-op when already done.
	Fails loud if the admin domain line is absent (a clone from the wrong/old
	snapshot).

	`run_setup=False` writes the line WITHOUT running production setup — the caller
	guarantees a later step regenerates nginx (site mode runs `bench rename-site`,
	which does production setup itself, so the admin vhost picks up the domain we set
	here in that same pass). Admin mode leaves the default (`run_setup=True`) so it
	regenerates the vhost inline.

	`update_site` (site mode) is the on-disk site dir whose `pilot_endpoint` — the
	admin URL the site calls Pilot back on — should be re-pointed at this FQDN. It
	was baked at new-site time as the `admin.localhost` placeholder (the real admin
	domain wasn't known then), so we rewrite it here now that it is. Passed the baked
	`site.local` before the rename, so the corrected value rides the rename into the
	FQDN dir."""
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
	if update_site:
		_update_pilot_endpoint(update_site, fqdn)
	if run_setup:
		_bench("setup", "production")


def _update_pilot_endpoint(site: str, admin_fqdn: str) -> None:
	"""Re-point a site's `pilot_endpoint` at the real admin FQDN in its
	`site_config.json`. The key is the admin URL the site calls Pilot back on; it is
	baked at new-site time (pilot new_site.py), when the admin domain is still the
	`admin.localhost` placeholder — so left untouched it would keep every deployed
	site calling `admin.localhost`. HTTPS: the admin console is fronted at its public
	FQDN by the edge proxy, which terminates TLS. Idempotent (re-runs write the same
	value). No-op if the config is missing (a clone we can't fix here fails louder
	downstream)."""
	config_path = os.path.join(SITES_DIR, site, "site_config.json")
	if not os.path.exists(config_path):
		return
	# nosemgrep: frappe-security-file-traversal -- guest script; reads a fixed site_config.json under the baked bench, not untrusted web input
	with open(config_path) as f:
		config = json.load(f)
	config["pilot_endpoint"] = f"https://{admin_fqdn}"
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the same fixed site_config.json path
	with open(config_path, "w") as f:
		json.dump(config, f, indent=1)


def _reissue_pilot_auth_token(fqdn: str) -> None:
	"""Re-issue the site's `pilot_auth_token` scoped to the FQDN in its
	`site_config.json`. The token is a JWT with `scope: "site"` and a `site` claim
	(pilot generate_session.has_scope), baked at new-site time (pilot new_site.py)
	scoped to the placeholder `site.local` — so after the rename to the FQDN the bench
	rejects it (`claims["site"] != <fqdn>`) and every site→bench API call 403s. Mint a
	fresh one for the FQDN with `bench admin issue-site-token <fqdn> --ttl <365d>` (same
	TTL as the bake) and write it back. Run AFTER the rename, against the FQDN dir.
	`issue-site-token` mints purely from the FQDN arg + bench.toml's jwt_secret and does
	not read the site off disk, so scoping to the FQDN is safe. Idempotent (a re-run
	just mints another valid token). No-op if the config is missing.

	The verb lives under the `admin` GROUP (`bench admin issue-site-token`) as of
	frappe/pilot v0.0.9-pre-alpha. Pilot registers a grouped command only under its
	group — there is no top-level alias — and its dispatcher treats an unrecognised
	leading verb as a FRAPPE PASSTHROUGH, so the old top-level spelling does not fail
	loudly as an unknown bench command: it is handed to Frappe, which rejects it. Goes
	through `_regrouped` so the pre-regrouping fork pin (where it is top-level) works
	too. Keep this in step with image_recipes._BENCH_CLI_REF."""
	config_path = os.path.join(SITES_DIR, fqdn, "site_config.json")
	if not os.path.exists(config_path):
		return
	token = _regrouped("admin", "issue-site-token", fqdn, "--ttl", str(365 * 24 * 3600), capture=True).strip()
	# nosemgrep: frappe-security-file-traversal -- guest script; reads a fixed site_config.json under the baked bench, not untrusted web input
	with open(config_path) as f:
		config = json.load(f)
	config["pilot_auth_token"] = token
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the same fixed site_config.json path
	with open(config_path, "w") as f:
		json.dump(config, f, indent=1)


# The local readiness paths, per bake mode, newest spelling first — a probe passes on
# the FIRST that answers 200. site mode serves a Frappe site whose built-in
# unauthenticated `/api/method/ping` returns 200; admin mode serves the pilot admin
# console — a FLASK app with NO `/api/method/ping` (it would 404) — whose
# unauthenticated health endpoint is `/api/v1/health` on upstream pilot
# (admin/backend/api/v1/core.py) and was `/api/status` on the pre-`/api/v1` fork, which
# upstream answers with a 404 `API route not found`. Both are tried so a golden of
# either vintage passes. Kept in lockstep with the controller's
# deploy_site.READINESS_PATH / readiness_paths_for_mode.
_HEALTH_PATHS = {
	"site": ("/api/method/ping",),
	"admin": ("/api/v1/health", "/api/status"),
}


def _serving(host_header: str, mode: str) -> bool:
	"""Best-effort in-guest confirmation that the front door answers locally before
	we report serving. The controller's wait_for_http is the authoritative gate
	(Contract B, end-to-end over the real network); this is a fast local sanity
	check so a deploy that silently failed to bring nginx up surfaces here too.

	Probe over **IPv6** (`[::1]`) AND v4 — the edge proxy reaches the VM over its
	public /128, so a v6 200 proves the path that matters is wired. The Host header
	is the FQDN (Contract A); in site mode the multitenant gunicorn resolves the
	renamed site from it, in admin mode nginx routes it to the admin app. The health
	PATH is mode-aware (the admin app has no Frappe ping route) and, in admin mode,
	spelling-tolerant — the first path that answers on BOTH stacks wins."""
	paths = _HEALTH_PATHS.get(mode, _HEALTH_PATHS["site"])
	return any(
		_local_ping(host_header, "[::1]", path) and _local_ping(host_header, "127.0.0.1", path)
		for path in paths
	)


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


def _regenerate_login(inputs: "DeploySiteInputs", log) -> None:
	"""Re-mint the one-click login URL for an ALREADY-deployed site and emit it —
	nothing else. The refresh Central drives when a tenant clicks after the current
	URL's short-lived token has expired (the admin JWT is 5 minutes, the site session
	24h). The FQDN is already on disk (site mode renamed it; admin mode set
	`[admin].domain`) and the stack is already serving, so this skips the whole
	front-door path (`_preflight`, warm freshen, `bench start`, rename / setup
	production, the Central-config write) and only touches the DB to sign a fresh
	session.

	We still gate on the bench DB accepting connections — the mint (`bench browse` in
	site mode, the admin session verb in admin mode) opens a `frappe.connect()`,
	and a regenerate can land on a VM that was just resumed from a memory snapshot with
	MariaDB still racing sshd. Emits the same `ATLAS_RESULT` shape as a full deploy
	(`serving` reflects the local probe) so the controller stamps it identically."""
	log(f"regenerate login (fqdn={inputs.site_name}, mode={inputs.mode})")
	log("waiting for the bench DB to accept connections …")
	_await_db_ready()
	log("bench DB ready")
	if inputs.mode == "admin":
		log("minting admin login URL (bench-cli admin session verb) …")
		login_url = _mint_admin_login_url()
	else:
		log("minting tenant login URL (bench browse) …")
		login_url = _mint_login_url(inputs.site_name)
	log("login URL minted")
	serving = _serving(inputs.site_name, inputs.mode)
	DeploySiteResult(site=inputs.site_name, serving=serving, login_url=login_url).emit()


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
	(snapshot-booted) idempotently re-asserts `bench start` first.

	`--regenerate-login` is the exception: the site is already deployed, so it skips
	every front-door step and only re-mints the login URL (see `_regenerate_login`)."""
	inputs = DeploySiteInputs.from_args()
	log = _stage_logger()
	if inputs.regenerate_login:
		_regenerate_login(inputs, log)
		return
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
		# The admin console's FQDN: the explicit --admin-domain when given, else the
		# site name (a stand-alone admin-mode VM is fronted at its own FQDN). Run
		# production setup inline so the admin vhost regenerates now.
		log("admin mode: pointing [admin].domain at the FQDN + setup production …")
		_set_admin_domain(inputs.admin_domain or inputs.site_name)
		log("admin vhost regenerated + reloaded")
		log("minting admin login URL (bench-cli admin session verb) …")
		login_url = _mint_admin_login_url()
		log("admin login URL minted")
	else:
		# Set `[admin].domain` to the attached console's FQDN FIRST (no production
		# setup — the rename below runs it), so the admin vhost is emitted in the same
		# rename-site pass as the site vhost. Whenever we know the admin FQDN we wire
		# it, so the console is reachable at its real host straight out of this deploy
		# (not left at the baked `admin.localhost` placeholder).
		if inputs.admin_domain:
			log(f"pointing [admin].domain at {inputs.admin_domain} (regenerates with the rename) …")
			# update_site=BAKED_SITE also re-points the baked site's `pilot_endpoint`
			# off the `admin.localhost` placeholder onto the real admin FQDN; the value
			# rides the rename below into the FQDN dir.
			_set_admin_domain(inputs.admin_domain, run_setup=False, update_site=BAKED_SITE)
		# `bench rename-site` moves the site, regenerates nginx, AND re-runs
		# production setup for the new domain in one step — so there is no separate
		# `bench setup nginx` here anymore. It is fast on a re-run / already-renamed
		# clone (production setup is idempotent).
		log("renaming baked site to the FQDN (bench rename-site) …")
		renamed = _rename_site_to_fqdn(inputs.site_name)
		log(f"rename {'done' if renamed else 'already in place'}")
		# The baked `pilot_auth_token` is a JWT scoped to `site.local` — dead after the
		# rename (the bench checks the `site` claim against the FQDN). Re-issue it for the
		# FQDN now that the site dir carries that name.
		log("re-issuing pilot_auth_token scoped to the FQDN …")
		_reissue_pilot_auth_token(inputs.site_name)
		log("pilot_auth_token re-issued")
		log("minting tenant login URL (bench browse) …")
		login_url = _mint_login_url(inputs.site_name)
		log("login URL minted")

	# Central handoff: seed the endpoint + single-use bootstrap token and enrol. `bench
	# admin enroll` exchanges the token for the pilot's long-lived credential + JWKS trust
	# config and writes them into bench.toml (Pilot owns that file). Only the short-lived
	# token is ever injected here — the durable secret is minted by the pilot itself.
	# Grouped under `admin` on current pilot, same as `admin issue-site-token` above —
	# and through the same `_regrouped` fallback, so a golden baked off an older tree
	# (where the verb was top-level, or absent entirely) still deploys, and one that
	# carries it at NO spelling fails loud here rather than reporting an enrolment that
	# never happened.
	if inputs.central_endpoint and inputs.bootstrap_token:
		log("enrolling with Central (bench admin enroll) …")
		_regrouped(
			"admin",
			"enroll",
			"--endpoint",
			inputs.central_endpoint,
			"--bootstrap-token",
			inputs.bootstrap_token,
		)
		log("enrolled with Central")

	log("local serving probe (v6 + v4) …")
	serving = _serving(inputs.site_name, inputs.mode)
	log(f"deploy complete (serving={serving})")
	result = DeploySiteResult(site=inputs.site_name, serving=serving, login_url=login_url)
	result.emit()


if __name__ == "__main__":
	main()
