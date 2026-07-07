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

# --- Pins. install.sh clones bench-cli's MOVING main; we check out the exact
# committed ref afterwards so the golden is reproducible (the same discipline
# proxy/build.sh follows). The Frappe branch + the production/MariaDB/ZFS shape
# are pinned in bench.toml.
#
# Pinned at 1e0332b (main @ 2026-07-03). This ref carries the five things this
# build/deploy flow now depends on: (1) the two-path install.sh — run as root it
# creates the bench user + sudoers, run as the user it installs bench-cli (so we no
# longer hand-roll useradd/sudoers); (2) `bench rename-site` (deploy-site.py renames
# the baked site through it — ABSENT before commit 0bc54f2, so an older pin breaks
# the deploy); (3) nginx emits `listen [::]:80` for every site + admin vhost (since
# dd14ad4), so the Atlas v6-only inbound path is served by bench-cli itself — no
# v6-listener / default_server surgery here; (4) `bench generate-admin-session`
# (Pilot #117, merged as 35ae14e) — the admin-mode login-URL handoff; (5) `bench
# set-central-config` (Pilot #150) — the Central endpoint/token handoff deploy-site.py
# writes into bench.toml. An older pin dies at that step with "No such command".
#
# BENCH_CLI_REF / ERPNEXT_BRANCH are ENV OVERRIDES: the controller
# (atlas.atlas.image_builder) exports them per recipe so one committed build.sh
# bakes any Frappe version (v15 / v16 / nightly). The Frappe branch + Python
# version are pinned in bench.toml (rendered by the controller before upload).
# The defaults below keep a direct `build.sh` run (no env) reproducible at v16. ---
BENCH_CLI_REF="${BENCH_CLI_REF:-486005a0beb949d1e02c316603f6d318c181620e}"  # default: main @ 2026-07-03 (fixes ?sid= login-url sign-in, PR #159)
ERPNEXT_BRANCH="${ERPNEXT_BRANCH:-version-16}"  # default: v16; controller overrides for v15 / develop

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
as_frappe() {
	sudo -u "$BENCH_USER" bash -lc "export PATH='$BENCH_CLI_DIR':\$PATH; export XDG_RUNTIME_DIR=/run/user/\$(id -u); cd '$BENCH_CLI_DIR' 2>/dev/null || cd '$BENCH_HOME'; $*"
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
# install only `zfsutils-linux` (zpool/zfs binaries), which bench-cli's VolumeManager
# needs to build the pool/datasets. This is the ONE ZFS thing build.sh does. ---
apt-get update
# `git` is bench-cli's own bootstrap dependency: install.sh (below) clones bench-cli
# with git, and bench pulls/updates apps over git at runtime. The standard Ubuntu base
# ships it, but the minimal base drops it to shrink the runtime surface — so install it
# here rather than assume the base carries it. It lands in the golden bench snapshot
# (a superset of the base), not the base image. Without it the bake dies at install.sh
# `Cloning bench-cli` with `git: command not found` (exit 127).
apt-get install -y --no-install-recommends zfsutils-linux git

# --- 3. Install bench-cli — install.sh creates the bench user too (bench-setup.md
# §3+§4). install.sh has two paths (bench-cli @ 03a4272 install.sh): run AS ROOT it
# creates the `$BENCH_USER` (`useradd -m`, adds to sudo) + writes a visudo-validated
# `/etc/sudoers.d/$BENCH_USER` (passwordless), then STOPS; run AS THAT USER it clones
# bench-cli to ~/bench-cli, installs uv + Node + tzdata, adds bench-cli to PATH, and
# sets up the .admin-venv (flask/psutil/pymysql/gunicorn). So we no longer hand-roll
# useradd/usermod/sudoers — the root call does it (no explicit uid; frappe takes the
# next free uid). We then check out the pinned ref so the golden is reproducible
# (install.sh tracks moving main).
#
# Idempotent: the root call is a no-op-ish re-run (user exists → skips useradd,
# rewrites the same sudoers); the user call runs install.sh only on a FRESH guest (no
# bench-cli dir yet) — a re-run must NOT re-invoke it, as install.sh `git pull`s to
# self-update and FATALs on the detached HEAD the pin below leaves ("not currently on
# a branch"). Re-running just re-fetches + re-pins the ref. ---
INSTALL_URL="https://raw.githubusercontent.com/frappe/pilot/$BENCH_CLI_REF/install.sh"
curl -fsSL "$INSTALL_URL" | bash -s -- --user "$BENCH_USER" -y

# Enable lingering for the bench user NOW that it exists. Current bench-cli runs
# the production stack (redis_queue/redis_cache, web, workers) as `systemctl --user`
# units; lingering starts that user's systemd manager at boot — without it the units
# only run inside a login session, so `bench setup production` can't bring them up at
# bake time AND the golden would not "boot serving" (the property [production] in
# bench.toml relies on). enable-linger also creates /run/user/<uid>, the bus path
# as_frappe exports as XDG_RUNTIME_DIR. Idempotent.
loginctl enable-linger "$BENCH_USER"

if [ ! -d "$BENCH_CLI_DIR/.git" ]; then
	as_frappe "curl -fsSL '$INSTALL_URL' | bash"
fi
as_frappe "git -C '$BENCH_CLI_DIR' fetch --quiet origin && git -C '$BENCH_CLI_DIR' checkout --quiet '$BENCH_CLI_REF'"

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
# up the per-bench substrate from bench.toml: the ZFS pool + datasets
# (volume.enabled), the DEDICATED mariadb@atlas instance (provisioned, secured,
# enabled-at-boot), the bench's Redis config, the uv venv, the Frappe clone, Node
# deps, the admin frontend, and dns_multitenant = 1.
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

# --- 5b. Install the in-guest domain provider (spec/18 Component D). The thin "push"
# half of one-way self-service subdomain routing, and the `bench-domain-provider`
# plug-in pilot (formerly bench-cli) discovers on PATH and drives by verb: the new-site
# flow runs `bench-domain-provider register <domain>` BEFORE creating the site (the
# authoritative reservation; pilot aborts on a non-zero exit) and `deregister <domain>`
# after drop / as the create-failure rollback; `wildcard-domains` / `proxy-servers`
# answer pilot's host-level queries (name constraint + the edge it locks nginx down to).
# Stdlib-only, so the stock guest python3 runs it; reads the ONE non-secret file
# /etc/atlas-routing.env the controller injects (no UUID, no token — caller resolution
# is by source address). No-ops cleanly (register exits 0, host queries print blank)
# when no routing config is present, so a non-Atlas bench is unaffected. Installed on
# EVERY golden (site + admin), since a bench in either mode can spin up routable sites.
# The binary name + path are the contract pilot looks up — keep them exactly. ---
install -m 0755 "$SRC_DIR/bench-domain-provider.py" /usr/local/bin/bench-domain-provider

# --- 6. Site mode only: bake a fully-created Frappe + ERPNext site, taking the
# heaviest per-signup costs (`bench new-site` + `install-app erpnext`) once here.
# admin mode bakes no site — the clone's domain maps to the admin app instead. ---
if [ "$MODE" = "site" ]; then
	# `get-app` clones ERPNext + builds its assets into the venv; it needs no
	# running bench. `new-site` only VALIDATES --apps (it does not install them),
	# so install-app erpnext is a separate, required step. install-app enqueues
	# background jobs, so Redis must be up: `bench start` brings the production
	# stack up (its systemd units), which we leave running for the rest of the bake.
	if [ ! -d "$BENCH_DIR/apps/erpnext" ]; then
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
		as_frappe "bench -b '$BENCH_NAME' new-site '$BAKED_SITE' --admin-password '$BAKED_ADMIN_PASSWORD' --apps erpnext"
		as_frappe "bench -b '$BENCH_NAME' frappe --site '$BAKED_SITE' install-app erpnext"
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

# --- 7. Stamp the resolved input commits. The Frappe branch (and ERPNext, and
# bench-cli's main) can be a MOVING target — `develop` for the nightly variant — so
# we record the exact commit each app was actually built from on `ATLAS_BUILD_*=`
# lines. These are captured in the `bench-build` Task's stdout, which the Image
# Build controller harvests into the build's audit (image_build.run), making even a
# nightly image traceable to its real inputs. `git -C` is cheap and the repos are
# right here in the bench. ---
git_sha() { git -C "$1" rev-parse HEAD 2>/dev/null || echo "unknown"; }
echo "ATLAS_BUILD_BENCH_CLI_REF=$(git_sha "$BENCH_CLI_DIR")"
echo "ATLAS_BUILD_FRAPPE_SHA=$(git_sha "$BENCH_DIR/apps/frappe")"
if [ "$MODE" = "site" ]; then
	echo "ATLAS_BUILD_ERPNEXT_SHA=$(git_sha "$BENCH_DIR/apps/erpnext")"
fi

# --- 8. Trim build cruft so golden copies are lean. The stack is LEFT RUNNING.
# The e2e re-asserts the bake over guest-SSH after the snapshot boots. ---
apt-get clean
rm -rf /var/lib/apt/lists/* "$BENCH_HOME/.cache" 2>/dev/null || true

echo "Golden bench image baked (mode=$MODE): bench-cli @ ${BENCH_CLI_REF:0:12}, bench '$BENCH_NAME'$([ "$MODE" = site ] && echo " + ERPNext site '$BAKED_SITE'"), production stack running."
