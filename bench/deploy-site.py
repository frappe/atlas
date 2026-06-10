#!/usr/bin/env python3
# Deploy ONE Frappe site into a golden bench VM — run INSIDE the guest over
# guest-SSH (plans/self-serve/03-deploy-site-script.md), driven by the controller
# `atlas.atlas.deploy_site.deploy_site`. The golden image (plans/self-serve/01)
# already baked bench-cli + `bench init` AND a fully-created site under the fixed
# name `site.local`; this script does only the per-site work the image can't bake
# because it carries the routing identity (Contract A): RENAME the baked site to
# `<fqdn>` + reset its Administrator password + the production bring-up so the
# bench's own nginx serves the site on :80.
#
# Why rename, not `bench new-site`: in bench-cli a site's identity IS its
# directory name under `sites/` (nginx's `server_name` is the dir name; Frappe
# resolves the Host header to `sites/<host>/`; the db name lives in that dir's
# `site_config.json` and travels with the move). So the per-site step is a
# sub-second directory move + an nginx regen, NOT the multi-minute schema create
# + frappe install `bench new-site` pays — that is baked once into the image.
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
# in). Idempotent (spec taste #14: retry = re-run): a site already at <fqdn> is
# left in place and re-asserted serving, never re-renamed or double-created.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass

RESULT_MARKER = "ATLAS_RESULT="

# Where build.sh installed bench-cli and the baked bench. Kept in lockstep with
# bench/build.sh (BENCH_CLI_DIR / BENCH_NAME) — if the bake moves them, move them
# here too. The site lives under <bench>/sites/<fqdn>.
BENCH_CLI_DIR = "/root/bench-cli"
BENCH_NAME = "atlas"
BENCH_DIR = f"{BENCH_CLI_DIR}/benches/{BENCH_NAME}"
BENCH = f"{BENCH_CLI_DIR}/bench"

# The site baked into the golden image (bench/build.sh BAKED_SITE). The per-site
# deploy renames this directory to the FQDN; a clone that doesn't carry it was
# built from the wrong/old (site-less) snapshot — _preflight fails loud on that.
BAKED_SITE = "site.local"
SITES_DIR = f"{BENCH_DIR}/sites"

# uv lands here; bench-cli shells out to it. A non-interactive SSH command does
# not source /etc/profile.d, so put the baked dirs on PATH explicitly.
BAKED_PATH = f"{BENCH_CLI_DIR}:/root/.local/bin"


@dataclass(frozen=True)
class DeploySiteInputs:
	"""Per-site deploy inputs. `site_name` is the full FQDN (Contract A) — the
	bench new-site name on disk, the proxy Host header, and the Site key, one
	string never transformed. `admin_password` is generated per-site by the
	controller (D01-3: the db root password is baked + shared, the Administrator
	password is per-site) and returned to nobody but the owner."""

	site_name: str
	admin_password: str

	@classmethod
	def from_args(cls, argv: list[str] | None = None) -> "DeploySiteInputs":
		parser = argparse.ArgumentParser(prog="deploy-site", description=cls.__doc__)
		parser.add_argument("--site-name", required=True, help="Full FQDN, e.g. acme.blr1.frappe.dev")
		parser.add_argument("--admin-password", required=True, help="Frappe Administrator password")
		ns = parser.parse_args(argv)
		return cls(site_name=ns.site_name, admin_password=ns.admin_password)


@dataclass(frozen=True)
class DeploySiteResult:
	"""What the controller records on the Site row. `created` is False on a
	re-run that found the site already present at `<fqdn>` (idempotency), True the
	first time the baked site was renamed into place — so the operator can tell a
	fresh deploy from a retry."""

	site: str
	created: bool
	serving: bool

	def emit(self) -> None:
		print(
			RESULT_MARKER + json.dumps({"site": self.site, "created": self.created, "serving": self.serving})
		)


def _env() -> dict:
	env = dict(os.environ)
	env["PATH"] = BAKED_PATH + ":" + env.get("PATH", "")
	env["DEBIAN_FRONTEND"] = "noninteractive"
	return env


def _run(args: list[str], *, capture: bool = False) -> str:
	"""Run a command, streaming to our stdout/stderr (so the controller's Task row
	captures the bench output) unless `capture`, in which case return stdout. Fail
	loud: a non-zero exit aborts the deploy (the controller marks the Site Failed)."""
	result = subprocess.run(
		args,
		env=_env(),
		text=True,
		capture_output=capture,
		check=True,
	)
	return result.stdout if capture else ""


def _bench(*args: str, capture: bool = False) -> str:
	"""Invoke the baked bench-cli against the baked bench (`bench -b atlas …`)."""
	return _run([BENCH, "-b", BENCH_NAME, *args], capture=capture)


def _preflight() -> None:
	"""Assert this is a golden bench VM, not plain Ubuntu. A missing bench-cli or
	baked bench means the VM was cloned from the wrong snapshot — fail loud before
	any per-site work (the wrong image is unrecoverable here, not retryable). The
	baked SITE is checked separately, in `_rename_site` only on a fresh deploy: an
	idempotent re-run whose site is already at `<fqdn>` has legitimately consumed
	(renamed) the baked site, so a missing `site.local` there is expected, not an
	error."""
	if not os.path.exists(BENCH):
		sys.exit(f"bench-cli not found at {BENCH}; this VM was not baked from the golden image (plan 01)")
	if not os.path.isdir(BENCH_DIR):
		sys.exit(f"baked bench {BENCH_DIR} missing; this VM was not baked from the golden image (plan 01)")


def _site_exists(site_name: str) -> bool:
	"""A site exists iff its on-disk dir is present under the bench. Cheap, and the
	idempotency predicate: re-running on a VM whose site was already renamed to the
	FQDN must not rename again (the baked site is gone, and re-renaming would
	clobber the live one)."""
	return os.path.isdir(f"{SITES_DIR}/{site_name}")


def _rename_site(inputs: DeploySiteInputs) -> None:
	"""Turn the baked `site.local` into the per-VM site at `<fqdn>` — the per-site
	work, done as a directory move, not a `bench new-site`.

	In bench-cli a site's identity IS its directory name under `sites/`: nginx's
	`server_name` is the dir name, Frappe resolves the Host header to
	`sites/<host>/`, and the db name lives in that dir's `site_config.json` (so it
	travels with the move — no DB rename needed). So Contract A's "name on disk IS
	the FQDN, verbatim" is satisfied by `os.rename(sites/site.local →
	sites/<fqdn>)`. The Administrator password is then reset to the per-VM secret:
	the baked password (build.sh) is a shared throwaway and must never reach a user
	(D01-3 — only the db root password is baked + shared). The setup-wizard gate is
	already cleared at bake time, so it is not re-set here."""
	baked = f"{SITES_DIR}/{BAKED_SITE}"
	if not os.path.isdir(baked):
		sys.exit(
			f"baked site {baked} missing; this VM was cloned from a site-less snapshot, "
			f"not the baked-site golden image (plan 01) — re-bake or fix default_bench_snapshot"
		)
	os.rename(baked, f"{SITES_DIR}/{inputs.site_name}")
	_reset_admin_password(inputs)


def _set_default_site(site_name: str) -> None:
	"""Repoint `sites/common_site_config.json` `default_site` from the baked
	`site.local` to the per-VM FQDN.

	This is load-bearing on the rename model. bench-cli's `frappe serve` does NOT
	resolve the site from the `Host` / `X-Frappe-Site-Name` header here (the
	`dns_multitenant` request router never engages for it on a snapshot-booted
	clone) — every request falls back to `default_site`. The bake leaves
	`default_site = site.local`; after the rename that directory is gone, so every
	request 404s "site.local does not exist" (proven on a real host). A site VM is
	single-tenant — one site per VM — so pointing `default_site` at the renamed
	FQDN is both the fix and the correct model. Rewritten in stdlib JSON (no jq in
	the guest); a missing key is simply added."""
	config_path = f"{SITES_DIR}/common_site_config.json"
	with open(config_path) as handle:
		config = json.load(handle)
	config["default_site"] = site_name
	with open(config_path, "w") as handle:
		json.dump(config, handle, indent=1)


def _reset_admin_password(inputs: DeploySiteInputs) -> None:
	"""`bench frappe --site <fqdn> set-admin-password <pw>` — replace the baked
	throwaway Administrator password with the per-VM secret the controller
	generated. Frappe resolves `--site` by directory name, so this runs against the
	just-renamed dir. The password crosses only the encrypted SSH channel (an argv
	flag), never a guest file."""
	_bench("frappe", "--site", inputs.site_name, "set-admin-password", inputs.admin_password)


def _setup_production() -> None:
	"""Bring the bench up production-style so its OWN nginx serves the site on :80.
	`bench setup production` generates + installs the nginx + supervisor config and
	reloads them. The bench's nginx is the in-guest front door; TLS still terminates
	at the EDGE proxy (plan 03 open Q — no in-guest certbot), which routes the south
	hop to this :80. Whole-bench, not per-site, so it is safe + idempotent to re-run.

	Note: bench-cli sets `dns_multitenant`, but on a snapshot-booted clone its
	`frappe serve` does NOT actually resolve the site from the `Host` header — every
	request falls back to `default_site` (proven on a real host). So `_set_default_site`
	(run before this, in `main`) is what makes the renamed site serve, not Host
	routing; this just brings nginx + supervisor up and `_restart_web` recycles
	gunicorn so it reads the new `default_site`."""
	_bench("setup", "production")
	_enable_ipv6_listeners()
	_restart_web()


def _restart_web() -> None:
	"""Restart the bench's supervisor-managed web process so gunicorn reloads
	`common_site_config.json`.

	`_set_default_site` rewrote `default_site` to the FQDN, but the gunicorn
	workers baked into the snapshot started at boot against the old `site.local`
	and cache the resolved default site in-process — `bench setup production`'s
	supervisor reload does not always recycle an already-running web worker. An
	explicit restart guarantees the new `default_site` takes effect (proven needed
	on a real host). Best-effort: a missing supervisor program name must not fail
	the deploy, so tolerate a non-zero exit."""
	subprocess.run(
		["sudo", "supervisorctl", "restart", f"{BENCH_NAME}:{BENCH_NAME}-web"],
		env=_env(),
		text=True,
		check=False,
	)


def _enable_ipv6_listeners() -> None:
	"""Make the bench's per-site nginx vhosts listen on IPv6, then reload.

	`bench setup production` emits site vhosts with a bare `listen 80;` — which
	binds IPv4 ONLY. But the EDGE proxy reaches each site over the VM's public
	**/128 (IPv6)** — the only inbound path (vm-inbound-ipv6-only). Without an
	explicit `listen [::]:80;` the site vhost never matches a v6 request, so the
	request falls through to whatever owns the v6 default — answering 404. The
	site serves fine on v4 and is dead on v6 (the path that matters): the south
	hop and the readiness probe both fail. So after every `setup production` we
	add `listen [::]:80;` beside each `listen 80;` in the generated site vhosts
	and reload. Idempotent — re-adding is guarded by a presence check.

	(The stock Ubuntu default vhost, which also grabbed [::]:80 default_server, is
	removed at bake time in build.sh — see that file. Both are required: remove the
	hijacker, and give the real vhost a v6 listener.)"""
	sites_dir = os.path.join(BENCH_DIR, "config", "nginx", "sites")
	if not os.path.isdir(sites_dir):
		# Older bench-cli layouts inline vhosts in config/nginx.conf; fall back to
		# patching that single file so the fix is layout-agnostic.
		candidates = [os.path.join(BENCH_DIR, "config", "nginx.conf")]
	else:
		candidates = [os.path.join(sites_dir, f) for f in os.listdir(sites_dir) if f.endswith(".conf")]
	for conf in candidates:
		_add_ipv6_listen(conf)
	_run(["sudo", "systemctl", "reload", "nginx"])


def _add_ipv6_listen(conf_path: str) -> None:
	"""Insert `listen [::]:80;` after each `listen 80;` in one nginx conf, unless
	a v6 listener is already present. Pure text edit — no nginx-config parser in
	the guest (stdlib-only, like the rest of this script)."""
	try:
		with open(conf_path) as f:
			text = f.read()
	except FileNotFoundError:
		return
	if "listen [::]:80" in text:
		return
	out_lines = []
	for line in text.splitlines(keepends=True):
		out_lines.append(line)
		stripped = line.strip()
		if stripped == "listen 80;" or stripped.startswith("listen 80 "):
			indent = line[: len(line) - len(line.lstrip())]
			out_lines.append(f"{indent}listen [::]:80;\n")
	with open(conf_path, "w") as f:
		f.write("".join(out_lines))


def _serving(site_name: str) -> bool:
	"""Best-effort in-guest confirmation that the site answers locally before we
	report serving. The controller's wait_for_http is the authoritative gate
	(Contract B, end-to-end over the real network); this is a fast local sanity
	check so a deploy that silently failed to bring nginx up surfaces here too.

	Probe over **IPv6** (`[::1]`), not just v4 — the edge proxy reaches the site
	over its public /128, so a v4-only vhost serves 200 on 127.0.0.1 while the
	real (v6) path 404s. Checking v6 here makes that failure surface in-guest
	instead of only at the controller's wait_for_http minutes later."""
	return _local_ping(site_name, "[::1]") and _local_ping(site_name, "127.0.0.1")


def _local_ping(site_name: str, host_ip: str) -> bool:
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
				f"http://{host_ip}:80/api/method/ping",
			],
			text=True,
			capture_output=True,
			timeout=30,
			check=False,
		)
		return out.stdout.strip() == "200"
	except Exception:
		return False


def main() -> None:
	inputs = DeploySiteInputs.from_args()
	_preflight()

	created = False
	if _site_exists(inputs.site_name):
		print(f"Site {inputs.site_name} already exists; re-asserting serving (idempotent re-run).")
	else:
		_rename_site(inputs)
		created = True

	# Always (re)point default_site at the FQDN — idempotent, and load-bearing even
	# on a re-run that skipped the rename (a deploy that renamed but died before
	# fixing default_site must self-heal on retry). See _set_default_site.
	_set_default_site(inputs.site_name)
	_setup_production()

	result = DeploySiteResult(site=inputs.site_name, created=created, serving=_serving(inputs.site_name))
	result.emit()


if __name__ == "__main__":
	main()
