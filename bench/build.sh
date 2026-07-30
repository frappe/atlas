#!/usr/bin/env bash
# Bake the golden bench image — run INSIDE a freshly-provisioned Ubuntu guest
# (spec/08-images.md § golden bench image), uploaded verbatim and run over
# guest-SSH by atlas.atlas.bench_image.build_bench (the sibling of
# proxy.build_proxy). This is the PROVEN recipe (llm/references/bench-setup.md)
# and nothing more: the whole production stack — MariaDB + Redis + nginx + the
# bench processes — is stood up and MANAGED by `bench init` + `bench start`,
# because bench.toml sets `process_manager = "systemd"`. bench-cli then installs
# `systemctl --user` units, `loginctl enable-linger`s the bench user, and
# enables the bench target, so a snapshot-booted clone comes back up serving
# with NO hand-rolled boot unit, ZFS drop-in, or nginx surgery. Everything the
# old build.sh hand-rolled is now bench-cli's job.
#
# Run as ROOT (the controller SSHes in as root). build.sh runs install.sh as root
# once to create the unprivileged `frappe` user (+ passwordless sudoers) the proven
# recipe uses, then runs install.sh and every bench step AS frappe — the systemd boot
# persistence (linger) is per-user, so it needs a real lingering non-root user, which
# is why root can't bake the bench itself.
#
# TWO MODES (first arg, default `site`):
#   * site  — bake a fully-created Frappe + ERPNext site under the fixed name
#             `site.local` and leave it serving. deploy-site.py `bench rename-site`s
#             `site.local` → `<fqdn>` per clone (rename + nginx + production setup
#             in one), so the DOMAIN MAPS TO THE SITE URL.
#   * admin — bake only the bench + the admin app (no site). deploy sets
#             `[admin].domain = <fqdn>` + `bench setup production` per clone, so the
#             DOMAIN MAPS TO THE ADMIN URL.
# Both modes share one recipe up to the site step; the mode only decides whether
# a site is baked. The per-clone rename / admin-domain mapping lives in
# deploy-site.py — `bench rename-site` (site) or `bench setup production` (admin)
# regenerates nginx to map either correctly.
#
# Idempotent (spec taste #16: retry = re-run): install.sh is clone-or-pull,
# `bench init` is idempotent, and every step below skips when its output exists.

set -euo pipefail

# --- Pilot install. The DEFAULT is the path pilot's README documents, verbatim:
#
#     curl -fsSL https://raw.githubusercontent.com/frappe/pilot/develop/install.sh | bash
#
# i.e. install.sh off `develop`, which lays down the LATEST release (prebuilt admin
# UI, no build step). EVERY recipe — the site line and the admin line — bakes through
# that one path, so a golden is what an operator following the README would get and a
# fresh environment needs no pin bumped to work. The Frappe branch and the production
# shape are pinned (bench.toml); the pilot version deliberately is not.
#
# What that path must carry for this build/deploy flow (all present upstream):
# (1) the two-path install.sh — run as root it creates the bench user + sudoers, run
# as the user it installs pilot (so we no longer hand-roll useradd/sudoers);
# (2) `bench rename-site` (deploy-site.py renames the baked site through it);
# (3) nginx emits `listen [::]:80` for every site + admin vhost, so the Atlas v6-only
# inbound path is served by pilot itself — no v6-listener surgery here; (4) the
# `bench admin` GROUP: `enroll` (the Central credential exchange), `set-central-config`
# and `issue-site-token`. A one-click admin session verb is NOT on that list — upstream
# pilot ships none, and the console's one-click sign-in is minted by CENTRAL instead (a
# `?sid=` JWT the bench verifies offline against the JWKS `bench admin enroll` writes),
# so deploy-site.py degrades to the baked `[admin].password` when no in-guest verb
# exists rather than failing the deploy.
#
# PILOT_INSTALL_REF / BENCH_CLI_REPO / BENCH_CLI_REF / ERPNEXT_BRANCH are ENV
# OVERRIDES: the controller (atlas.atlas.image_builder) exports them per recipe so one
# committed build.sh bakes any Frappe version (v15 / v16 / nightly). The Frappe branch
# + Python version are pinned in bench.toml (rendered by the controller before upload).
# The defaults below keep a direct `build.sh` run (no env) on the documented path. ---
# The repo install.sh is fetched from, and installs the releases of. BENCH_CLI_REPO
# travels with BENCH_CLI_REF — a ref only resolves against the repo it lives in.
BENCH_CLI_REPO="${BENCH_CLI_REPO:-frappe/pilot}"
# The git ref install.sh ITSELF is fetched at — `develop` is the README's URL. This is
# not a version pin: the release path installs the newest release whatever it says.
PILOT_INSTALL_REF="${PILOT_INSTALL_REF:-develop}"
# EMPTY BY DEFAULT = no version pin, which is what "the documented install" means.
# install.sh's release path has no way to request a specific release — it always
# fetches the newest — so a pin here was never enforceable, only detectable after the
# fact, and asserting it turned every upstream release into a broken bake (three in a
# row: wanted v0.0.9 got v0.0.14, then wanted v0.0.14 got v0.0.15 mid-run). Set it to
# pin a FORK's commit (the git install shape, §3b), or to make a release bake NOTE when
# upstream has moved past the version you expected. Either way the golden RECORDS the
# version it actually got (ATLAS_BUILD_BENCH_CLI_REF, §7), so an image is always
# identifiable after the fact even though it is not pinnable in advance.
BENCH_CLI_REF="${BENCH_CLI_REF:-}"
ERPNEXT_BRANCH="${ERPNEXT_BRANCH:-version-16}"  # default: v16; controller overrides for v15 / develop
# Bake ERPNext into the golden? OFF by default: `get-app erpnext` + `install-app` is
# by far the longest phase of the bake (clone + asset build + a full app install on
# the baked site), and a Frappe-only golden is a complete, serving bench — sites and
# the admin console work identically without it. Set INCLUDE_ERPNEXT=1 to bake the
# ERPNext-bearing golden back.
INCLUDE_ERPNEXT="${INCLUDE_ERPNEXT:-0}"

BENCH_USER="frappe"
BENCH_HOME="/home/$BENCH_USER"
# The bench-cli repo was renamed frappe/bench-cli → frappe/pilot on main after
# PR #117; its install.sh (fc89e51+) now clones to ~/pilot, not ~/bench-cli. The
# variable keeps its name (bench-cli is still the CLI's colloquial name across the
# tree), only the on-disk path follows the rename. Kept in lockstep with
# deploy-site.py's BENCH_CLI_DIR and warm.sh's.
BENCH_CLI_DIR="$BENCH_HOME/pilot"
BENCH_NAME="atlas"
BENCH_DIR="$BENCH_CLI_DIR/benches/$BENCH_NAME"

# The baked site (site mode only). A clone already carries a fully-created
# Frappe + ERPNext site under this name; deploy-site.py renames it to the per-VM
# FQDN at deploy time (a directory move, not a `bench new-site`). Kept in lockstep
# with bench/deploy-site.py's BAKED_SITE and warm.sh's BAKED_SITE.
BAKED_SITE="site.local"
# The baked Administrator password — a long random secret, generated ONCE here at
# bake time and never printed or exported off the golden. Every warm clone
# inherits the same unknown password; the tenant never needs it (they land via
# deploy-site.py's minted `sid`, see bench/deploy-site.py). Kept out of the build
# log: `new-site` receives it as an argv value, not echoed anywhere below.
BAKED_ADMIN_PASSWORD="$(openssl rand -hex 32)"

MODE="${1:-site}"
case "$MODE" in
	site | admin) ;;
	*)
		echo "usage: build.sh [site|admin]  (got: $MODE)" >&2
		exit 1
		;;
esac

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

# Run a command as the bench user through a LOGIN shell, so the uv/Node env
# install.sh set up is in place — exactly how an interactive operator following
# bench-setup.md reaches `bench`. We prepend bench-cli to PATH explicitly rather
# than rely on the `export PATH=…/pilot` line install.sh appends to ~/.bashrc:
# `bash -lc` is NON-interactive, and Ubuntu's stock ~/.bashrc returns at its top
# (`case $- in *i*) ;; *) return;; esac`) for non-interactive shells, BEFORE that
# export ever runs — so the login shell would otherwise not see `bench` at all
# (the bake hit exactly this: `bench new` → "command not found", exit 127). cd
# into the bench-cli dir when it exists (it does for every call after install.sh);
# the install.sh call itself runs from $HOME, before the dir exists.
#
# We also export XDG_RUNTIME_DIR (the bench user's /run/user/<uid>): current
# bench-cli runs the production stack as `systemctl --user` units, and every
# `systemctl --user` call needs the user bus path this points at. A bare
# `sudo -u` login shell does NOT set it, so without this `bench setup production`
# / `bench start` fail with "Failed to connect to bus: No medium found" and the
# redis_queue/redis_cache units never come up (install-app then dies on
# "Connection refused @ localhost:11000"). Lingering (enabled in §3) is what makes
# /run/user/<uid> exist outside a login session.
# NODE_OPTIONS raises Node's old-space cap for every command run here. Node defaults
# to roughly 2 GB regardless of how much RAM the box has, and the git (PILOT_DEV)
# install compiles the admin frontend from source — a Rollup/Vite build over the full
# dependency tree that blows straight through that default and aborts with
# `FATAL ERROR: Reached heap limit ... JavaScript heap out of memory` (npm exit 134),
# after which bench init rolls the whole bench back. The release install shape never
# hit this because it ships the frontend prebuilt. Set on the wrapper rather than the
# one call so the bench's own asset builds get the same headroom. 4 GB against the
# build VM's 6 GB leaves room for the rest of the bake.
as_frappe() {
	sudo -u "$BENCH_USER" bash -lc "export PATH='$BENCH_CLI_DIR':\$PATH; export XDG_RUNTIME_DIR=/run/user/\$(id -u); export NODE_OPTIONS=\"\${NODE_OPTIONS:---max-old-space-size=4096}\"; cd '$BENCH_CLI_DIR' 2>/dev/null || cd '$BENCH_HOME'; $*"
}

# --- 1. Fix setuid bits (bench-setup.md §1). The Ubuntu cloud rootfs is
# normalized at sync time; restore the setuid bits the privilege tools need so
# the frappe user's `sudo` works. ---
chmod u+s /usr/bin/sudo /usr/bin/passwd /usr/bin/su /bin/su \
	/usr/bin/chsh /usr/bin/newgrp /usr/bin/mount /bin/mount

# --- 2. Install the ZFS USERSPACE (bench-setup.md §2). The zfs.ko KERNEL module is
# baked into the guest rootfs at sync time (scripts/sync-image.py _install_guest_modules
# copies the PREBUILT zfs.ko + spl.ko from the manifest-pinned linux-modules-<kver>
# and pins them in modules-load.d), so build.sh no longer touches the module — that
# derives kver from the manifest, immune to the `uname -r` of this build VM. Here we
# install only `zfsutils-linux` (zpool/zfs binaries), which a VolumeManager needs to
# build the pool/datasets from bench.toml's `[volume]` tables. Upstream pilot has no
# volume schema at all, so with every recipe now on the documented upstream install
# NOTHING reads those tables and this package is installed and never used — kept
# because the module + userspace pair is what a volume-aware pin would need and
# re-deriving it is the fiddly part. This is the ONE ZFS thing build.sh does. ---
apt-get update
# `git` is bench-cli's own bootstrap dependency: install.sh (below) clones bench-cli
# with git, and bench pulls/updates apps over git at runtime. The standard Ubuntu base
# ships it, but the minimal base drops it to shrink the runtime surface — so install it
# here rather than assume the base carries it. It lands in the golden bench snapshot
# (a superset of the base), not the base image. Without it the bake dies at install.sh
# `Cloning bench-cli` with `git: command not found` (exit 127).
apt-get install -y --no-install-recommends zfsutils-linux git

# --- 3. Install pilot — install.sh creates the bench user too (bench-setup.md
# §3+§4). install.sh has two passes: run AS ROOT it creates the `$BENCH_USER`
# (`useradd -m`, adds to sudo) + writes a visudo-validated
# `/etc/sudoers.d/$BENCH_USER` (passwordless), then STOPS; run AS THAT USER it lays
# the CLI down under ~/pilot, installs uv + Node + tzdata, adds it to PATH, and sets
# up the .admin-venv (flask/psutil/pymysql/gunicorn). So we no longer hand-roll
# useradd/usermod/sudoers — the root call does it (no explicit uid; frappe takes the
# next free uid).
#
# Both calls are the README's command with `--user` added, which is the only thing
# the README itself says to vary ("On a root-only VPS, run it as root; the installer
# creates a non-root bench user"). No `--dev`: pilot's README scopes that to
# contributors who want to compile the admin UI locally, and that build OOMs Node on
# a build VM (npm exit 134, bench rolled back) — the release tarball ships the UI
# prebuilt and needs no build step, which is exactly what a bake wants.
#
# Idempotent: the root call is a no-op-ish re-run (user exists → skips useradd,
# rewrites the same sudoers); the user call runs install.sh only on a FRESH guest, per
# the entrypoint gate below. ---
INSTALL_URL="https://raw.githubusercontent.com/$BENCH_CLI_REPO/$PILOT_INSTALL_REF/install.sh"
curl -fsSL "$INSTALL_URL" | bash -s -- --user "$BENCH_USER"

# Enable lingering for the bench user NOW that it exists. Current bench-cli runs
# the production stack (redis_queue/redis_cache, web, workers) as `systemctl --user`
# units; lingering starts that user's systemd manager at boot — without it the units
# only run inside a login session, so `bench setup production` can't bring them up at
# bake time AND the golden would not "boot serving" (the property [production] in
# bench.toml relies on). enable-linger also creates /run/user/<uid>, the bus path
# as_frappe exports as XDG_RUNTIME_DIR. Idempotent.
loginctl enable-linger "$BENCH_USER"

# Gate on the CLI entrypoint, not on `.git`: a RELEASE install never leaves a `.git`,
# so a `.git` test would re-run install.sh on every re-bake — and on a git install a
# re-run is exactly what must not happen (it `git pull`s to self-update and FATALs on
# the detached HEAD a pin leaves, "not currently on a branch"). `bench` exists at the
# tree root under both install shapes. This is the call that installs the CLI tree
# into $BENCH_CLI_DIR; the root call above only provisions the bench user and the
# system packages.
if [ ! -x "$BENCH_CLI_DIR/bench" ]; then
	as_frappe "curl -fsSL '$INSTALL_URL' | bash"
fi

# --- 3b. Record (or, when asked, pin) the CLI version. install.sh ships TWO shapes
# and which one we got decides what can be done here:
#
#   release  (the DEFAULT, and the documented path) — install.sh downloads the
#            `pilot.tar.gz` asset of the LATEST release and untars it: no git, nothing
#            to check out, and the version is whatever was newest at bake time. It
#            ships a VERSION file, so RECORD that. With BENCH_CLI_REF set we also NOTE
#            the drift — never fail on it: the release path has no way to request a
#            specific release, so a tag pin was only ever detectable after the fact,
#            and asserting it turned every upstream release into a broken bake (three
#            in a row: wanted v0.0.9 got v0.0.14, then wanted v0.0.14 got v0.0.15
#            mid-run).
#   git      (a `--dev` install, or a fork whose install.sh clones) — install.sh
#            `git clone`s the repo. origin is hardcoded to frappe/pilot, so re-point
#            it at BENCH_CLI_REPO (a fork SHA is unreachable otherwise) and check
#            BENCH_CLI_REF out. Idempotent: set-url is safe on a re-run. Skipped
#            entirely when BENCH_CLI_REF is empty — nothing was asked for, so the
#            clone's own branch stands.
INSTALLED_VERSION="$(cat "$BENCH_CLI_DIR/VERSION" 2>/dev/null || true)"
if [ -d "$BENCH_CLI_DIR/.git" ] && [ -n "$BENCH_CLI_REF" ]; then
	# --tags: install.sh clones a single branch (develop), so a pin expressed as a
	# release TAG is not present until it is fetched explicitly. Without this a tag
	# pin dies on `pathspec ... did not match` even though the tag exists upstream.
	as_frappe "git -C '$BENCH_CLI_DIR' remote set-url origin 'https://github.com/$BENCH_CLI_REPO' && git -C '$BENCH_CLI_DIR' fetch --quiet --tags origin && git -C '$BENCH_CLI_DIR' checkout --quiet '$BENCH_CLI_REF'"
elif [ -n "$BENCH_CLI_REF" ] && [ "$INSTALLED_VERSION" != "$BENCH_CLI_REF" ]; then
	echo "NOTE: pilot ref '$BENCH_CLI_REF' requested; install.sh delivered '${INSTALLED_VERSION:-<no VERSION file>}' (the release path always ships latest). Baking with the delivered version." >&2
fi
echo "pilot version: ${INSTALLED_VERSION:-<git checkout>}"

# --- 4. Create the bench + drop our pinned bench.toml (bench-setup.md §5).
# `bench new` scaffolds benches/<name>/ non-interactively (name positional, no
# prompts); we overwrite its generated bench.toml with the committed one so the
# image's config is ours, not bench-cli's template. Idempotent: skip `bench new`
# if the bench dir already exists; the toml copy is an overwrite either way. ---
if [ ! -f "$BENCH_DIR/bench.toml" ]; then
	as_frappe "bench new '$BENCH_NAME'"
fi
install -m 0644 -o "$BENCH_USER" -g "$BENCH_USER" "$SRC_DIR/bench.toml" "$BENCH_DIR/bench.toml"

# The committed bench.toml carries a placeholder [admin].password (bench-cli
# refuses to start the admin app with none set). Replace it with a long random
# secret ONCE, generated here at bake time and never printed — mirrors
# BAKED_ADMIN_PASSWORD above. Admin mode's `bench generate-admin-session`
# (Pilot #117) is the tenant handoff (bench/deploy-site.py), so this password is
# never surfaced either. Idempotent: only replace the known placeholder, so a
# re-bake does not clobber an already-randomized password from a prior run.
if grep -q '^password = "admin-password"$' "$BENCH_DIR/bench.toml"; then
	admin_password="$(openssl rand -hex 32)"
	sed -i "s/^password = \"admin-password\"\$/password = \"$admin_password\"/" "$BENCH_DIR/bench.toml"
fi

# --- 5. `bench init` (bench-setup.md §6). The heavy, idempotent step that sets
# up the per-bench substrate from bench.toml: MariaDB (provisioned + secured), the
# bench's Redis config, the uv venv, the Frappe clone, Node deps, the admin frontend,
# and dns_multitenant = 1.
#
# MariaDB. Upstream pilot provisions ONE rootless, user-owned `pilot-mariadb.service`
# shared by the host's benches, datadir + socket under $BENCH_CLI_DIR/databases/mariadb
# — it reads no `[mariadb]` table from bench.toml (that config comes from the host
# common config) and has no volume/ZFS schema at all, so both tables are inert on every
# recipe now. deploy-site.py stays out of the unit-name argument entirely by probing the
# DB SOCKET rather than any unit name (its DB_SOCKET), so it works against any pin.
#
# `bench init` does NOT bring the production stack up: in current bench-cli the
# production `systemctl --user` units (redis_queue/redis_cache, web, workers, nginx)
# are installed + enabled by a SEPARATE `bench setup production` (run per mode in §6
# below), even though [production] is configured here. `bench start` only checks and
# reports "systemd deployment is incomplete" if they are absent.
#
# This is the HEADLESS bake path: `bench init` does its setup non-interactively from
# bench.toml. (The interactive `bench start` → browser setup-wizard flow in
# bench-setup-manual.md is for an operator at a terminal; a bake has no browser.) The
# old `source .admin-venv/bin/activate` pymysql workaround is gone — current bench-cli
# runs `bench init` inside its managed admin venv itself, so pymysql is found without
# a manual activate. ---
as_frappe "bench -b '$BENCH_NAME' init"

# --- 5a. Install `tzdata` into the bench venv. bench.toml pins python = "3.14", so
# `bench init` builds the venv on a uv-managed standalone CPython. Unlike a distro
# python (which reads /usr/share/zoneinfo), the standalone build ships NO zoneinfo
# database and relies on the pip `tzdata` package. Frappe declares no tzdata dep, so
# without this any `ZoneInfo(get_system_timezone())` call — e.g. `now_datetime()` in
# the setup wizard — dies with `ZoneInfoNotFoundError` (notably for legacy aliases
# like `Asia/Calcutta`). Bake it into the golden venv so every site has it. ---
as_frappe "cd '$BENCH_DIR' && uv pip install --python env/bin/python tzdata"

# --- 6. Site mode only: bake a fully-created Frappe + ERPNext site, taking the
# heaviest per-signup costs (`bench new-site` + `install-app erpnext`) once here.
# admin mode bakes no site — the clone's domain maps to the admin app instead. ---
if [ "$MODE" = "site" ]; then
	# `get-app` clones ERPNext + builds its assets into the venv; it needs no
	# running bench. `new-site` only VALIDATES --apps (it does not install them),
	# so install-app erpnext is a separate, required step. install-app enqueues
	# background jobs, so Redis must be up: `bench start` brings the production
	# stack up (its systemd units), which we leave running for the rest of the bake.
	if [ "$INCLUDE_ERPNEXT" = "1" ] && [ ! -d "$BENCH_DIR/apps/erpnext" ]; then
		as_frappe "bench -b '$BENCH_NAME' get-app https://github.com/frappe/erpnext --branch '$ERPNEXT_BRANCH'"
	fi

	# Bring the production stack up. `install-app erpnext` below enqueues background
	# jobs, so redis_queue (11000) + redis_cache (13000) must be serving first. In
	# current bench-cli the production units (redis, web, workers) are installed and
	# enabled by `bench setup production`, NOT by `bench init` or `bench start` —
	# `start` only reports "systemd deployment is incomplete" if they are absent. So
	# we run `setup production` here (idempotent; ~17s on a re-run). Combined with the
	# bench user's linger + XDG_RUNTIME_DIR (set above), this is what actually starts
	# the `systemctl --user` redis units the rest of the bake depends on.
	as_frappe "bench -b '$BENCH_NAME' setup production"

	# Block until redis_queue is actually accepting connections before install-app —
	# `setup production` returns once the units are started, but the socket may lag a
	# beat, and a race here resurfaces the exact "Connection refused @ 11000" the
	# stack was brought up to avoid.
	for _ in $(seq 1 30); do
		ss -ltn 2>/dev/null | grep -q ':11000' && break
		sleep 1
	done
	if ! ss -ltn 2>/dev/null | grep -q ':11000'; then
		echo "redis_queue (11000) did not come up after setup production" >&2
		ss -ltnp 2>/dev/null | grep -E ':(11000|13000|6379)' >&2 || true
		exit 1
	fi

	if [ ! -d "$BENCH_DIR/sites/$BAKED_SITE" ]; then
		if [ "$INCLUDE_ERPNEXT" = "1" ]; then
			as_frappe "bench -b '$BENCH_NAME' new-site '$BAKED_SITE' --admin-password '$BAKED_ADMIN_PASSWORD' --apps erpnext"
			as_frappe "bench -b '$BENCH_NAME' frappe --site '$BAKED_SITE' install-app erpnext"
		else
			as_frappe "bench -b '$BENCH_NAME' new-site '$BAKED_SITE' --admin-password '$BAKED_ADMIN_PASSWORD'"
		fi
		as_frappe "bench -b '$BENCH_NAME' frappe --site '$BAKED_SITE' migrate"
	fi

	# Regenerate nginx now that the site exists (new-site already did, but a
	# re-run / idempotent path makes this explicit) and assert the baked site
	# answers locally before we let the VM be snapshotted.
	as_frappe "bench -b '$BENCH_NAME' setup nginx"

	for _ in $(seq 1 60); do
		curl -sf -o /dev/null -H "Host: $BAKED_SITE" http://127.0.0.1/api/method/ping && break
		sleep 1
	done
	ping_body="$(curl -s -m 10 -H "Host: $BAKED_SITE" http://127.0.0.1/api/method/ping || true)"
	if [[ "$ping_body" != *pong* ]]; then
		echo "serve check FAILED: ping returned: $ping_body" >&2
		exit 1
	fi
else
	# admin mode: bring the production stack up (admin app + nginx) and leave it
	# running for the snapshot. The admin vhost is wired per-clone (deploy sets
	# [admin].domain + `bench setup nginx`), so there is nothing to assert here
	# beyond the stack being up. As in site mode, `setup production` (not `start`)
	# is what installs+enables the systemd --user units in current bench-cli.
	as_frappe "bench -b '$BENCH_NAME' setup production"
fi

# --- 6a. Enable nginx for boot. `bench setup production` START*s* nginx but never
# ENABLE*s* it (pilot's NginxManager only ever runs start/stop/reload — v0.0.9's
# reload_or_start picks `reload` when running and `start` when not, and nothing in
# pilot calls `systemctl enable nginx`). The Ubuntu package ships the unit `disabled`,
# so a running-but-disabled nginx survives only until the next boot — and this VM is
# ALWAYS rebooted before capture (image_build.run resizes it down from the fat build
# size to the restore size). The golden then boots with gunicorn up on 127.0.0.1:8000
# and nothing on :80, which is exactly the "does not serve (readiness HTTP 000)"
# post-build sanity failure. The bench's own units are `systemctl --user` and are
# already persisted by `setup production` + lingering; nginx is the one SYSTEM unit in
# the serving path, so it needs this. Idempotent, and correct for either bench-cli pin.
systemctl enable nginx
systemctl start nginx

# --- 6b. Install the in-guest domain provider (spec/18 Component D), AFTER the site
# is baked. The thin "push" half of one-way self-service subdomain routing, and the
# `bench-domain-provider` plug-in pilot (formerly bench-cli) discovers on PATH and
# drives by verb: the new-site flow runs `bench-domain-provider register <domain>`
# BEFORE creating the site (the authoritative reservation; pilot aborts on a non-zero
# exit) and `deregister <domain>` after drop / as the create-failure rollback;
# `wildcard-domains` / `proxy-servers` answer pilot's host-level queries (name
# constraint + the edge it locks nginx down to).
#
# It is installed AFTER the bake's own `bench new-site` (not before) on purpose: pilot's
# new-site gates `register` + the `matches_wildcard` name check on the provider being on
# PATH (DomainRouteProvider._host_query / _ask_provider `which()` it). The baked site is
# named `site.local`, which does NOT match a region wildcard `*.<region>.frappe.dev`, so
# a provider present at bake would make pilot reject it (or spuriously `register` it). By
# installing here the bake's new-site sees no provider and skips both — the golden still
# carries the binary, so live clones (which DO get /etc/atlas-routing.env) route normally.
#
# Stdlib-only, so the stock guest python3 runs it; reads the ONE non-secret file
# /etc/atlas-routing.env the controller injects (no UUID, no token — caller resolution
# is by source address). No-ops cleanly (register exits 0, host queries print blank)
# when no routing config is present, so a non-Atlas bench is unaffected. Installed on
# EVERY golden (site + admin), since a bench in either mode can spin up routable sites.
# The binary name + path are the contract pilot looks up — keep them exactly. ---
install -m 0755 "$SRC_DIR/bench-domain-provider.py" /usr/local/bin/bench-domain-provider

# --- 7. Stamp the resolved input commits. The Frappe branch (and ERPNext, and
# bench-cli's main) can be a MOVING target — `develop` for the nightly variant — so
# we record the exact commit each app was actually built from on `ATLAS_BUILD_*=`
# lines. These are captured in the `bench-build` Task's stdout, which the Image
# Build controller harvests into the build's audit (image_build.run), making even a
# nightly image traceable to its real inputs. `git -C` is cheap and the repos are
# right here in the bench. ---
git_sha() { git -C "$1" rev-parse HEAD 2>/dev/null || echo "unknown"; }
# The bench-cli tree is only a git checkout under a git install (§3b); a release
# install has no HEAD to read, so fall back to the VERSION file the tarball ships.
# Without this the audit would record a bare "unknown" for the one input the whole
# golden is pinned on.
bench_cli_stamp() {
	git -C "$BENCH_CLI_DIR" rev-parse HEAD 2>/dev/null && return
	cat "$BENCH_CLI_DIR/VERSION" 2>/dev/null || echo "unknown"
}
echo "ATLAS_BUILD_BENCH_CLI_REF=$(bench_cli_stamp)"
echo "ATLAS_BUILD_FRAPPE_SHA=$(git_sha "$BENCH_DIR/apps/frappe")"
if [ "$MODE" = "site" ] && [ "$INCLUDE_ERPNEXT" = "1" ]; then
	echo "ATLAS_BUILD_ERPNEXT_SHA=$(git_sha "$BENCH_DIR/apps/erpnext")"
fi

# --- 8. Trim build cruft so golden copies are lean. The stack is LEFT RUNNING.
# The e2e re-asserts the bake over guest-SSH after the snapshot boots. ---
apt-get clean
rm -rf /var/lib/apt/lists/* "$BENCH_HOME/.cache" 2>/dev/null || true

# --- 9. Flush every write to disk before we hand the VM back to be snapshotted.
# The controller stops the build VM with a plain `systemctl stop` of the firecracker
# unit — that terminates the guest, it does NOT ACPI-shut-it-down, so the guest never
# runs its own `sync`. Any file still dirty in the guest page cache at that instant is
# LOST: ext4 journals the inode + dirent but not the data, so the snapshot captures the
# file as 0 bytes. bench-domain-provider (installed in §6b, the LAST real write of the
# bake) hit exactly this — every image baked after the §6b move snapshotted a 0-byte
# provider, which fails at deploy with `Exec format error`. A single `sync` here makes
# the whole bake durable before the stop regardless of what wrote last. ---
sync

echo "Golden bench image baked (mode=$MODE): pilot @ $(bench_cli_stamp), bench '$BENCH_NAME'$([ "$MODE" = site ] && echo " + site '$BAKED_SITE'"), production stack running."
