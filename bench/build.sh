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
# Run as ROOT (the controller SSHes in as root). build.sh creates the unprivileged
# `frappe` user the proven recipe uses and runs every bench step AS frappe — the
# systemd boot persistence (linger) is per-user, so it needs a real lingering
# non-root user, which is why root can't bake this.
#
# TWO MODES (first arg, default `site`):
#   * site  — bake a fully-created Frappe + ERPNext site under the fixed name
#             `site.local` and leave it serving. deploy-site.py renames
#             `sites/site.local` → `sites/<fqdn>` + `bench setup nginx` per clone,
#             so the DOMAIN MAPS TO THE SITE URL.
#   * admin — bake only the bench + the admin app (no site). deploy sets
#             `[admin].domain = <fqdn>` + `bench setup nginx` per clone, so the
#             DOMAIN MAPS TO THE ADMIN URL.
# Both modes share one recipe up to the site step; the mode only decides whether
# a site is baked. The per-clone rename / admin-domain mapping lives in
# deploy-site.py — after a warm clone we rename the admin domain and mv the site,
# and `bench setup nginx` regenerates nginx to map either correctly.
#
# Idempotent (spec taste #16: retry = re-run): install.sh is clone-or-pull,
# `bench init` is idempotent, and every step below skips when its output exists.

set -euo pipefail

# --- Pins. install.sh clones bench-cli's MOVING main; we check out the exact
# committed ref afterwards so the golden is reproducible (the same discipline
# proxy/build.sh follows). The Frappe branch + the production/MariaDB/ZFS shape
# are pinned in bench.toml.
#
# Pinned at/after dd14ad4 "Serve sites and admin over IPv6" — that commit makes
# bench-cli's nginx emit `listen [::]:80` for every site + admin vhost, so the
# Atlas v6-only inbound path (the proxy/probe reach the VM over its public /128)
# is served by bench-cli itself. No v6-listener / default_server surgery here. ---
BENCH_CLI_REF="f36a06c541162aec80dd7b9894ccb4691597b9d3"  # main @ 2026-06-18 (incl. IPv6 listeners dd14ad4)

BENCH_USER="frappe"
BENCH_HOME="/home/$BENCH_USER"
BENCH_CLI_DIR="$BENCH_HOME/bench-cli"
BENCH_NAME="atlas"
BENCH_DIR="$BENCH_CLI_DIR/benches/$BENCH_NAME"
ADMIN_VENV="$BENCH_CLI_DIR/.admin-venv"

# The baked site (site mode only). A clone already carries a fully-created
# Frappe + ERPNext site under this name; deploy-site.py renames it to the per-VM
# FQDN at deploy time (a directory move, not a `bench new-site`). Kept in lockstep
# with bench/deploy-site.py's BAKED_SITE and warm.sh's BAKED_SITE.
BAKED_SITE="site.local"
# The baked Administrator password — a SHARED throwaway, the SAME on every clone;
# the owner is handed it and rotates it after first login. Kept in lockstep with
# warm.sh and the controller's Site.BAKED_ADMIN_PASSWORD.
BAKED_ADMIN_PASSWORD="$BENCH_NAME-baked"

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
# than rely on the `export PATH=…/bench-cli` line install.sh appends to ~/.bashrc:
# `bash -lc` is NON-interactive, and Ubuntu's stock ~/.bashrc returns at its top
# (`case $- in *i*) ;; *) return;; esac`) for non-interactive shells, BEFORE that
# export ever runs — so the login shell would otherwise not see `bench` at all
# (the bake hit exactly this: `bench new` → "command not found", exit 127). cd
# into the bench-cli dir when it exists (it does for every call after install.sh);
# the install.sh call itself runs from $HOME, before the dir exists.
as_frappe() {
	sudo -u "$BENCH_USER" bash -lc "export PATH='$BENCH_CLI_DIR':\$PATH; cd '$BENCH_CLI_DIR' 2>/dev/null || cd '$BENCH_HOME'; $*"
}

# --- 1. Fix setuid bits (bench-setup.md §1). The Ubuntu cloud rootfs is
# normalized at sync time; restore the setuid bits the privilege tools need so
# the frappe user's `sudo` works. ---
chmod u+s /usr/bin/sudo /usr/bin/passwd /usr/bin/su /bin/su \
	/usr/bin/chsh /usr/bin/newgrp /usr/bin/mount /bin/mount

# --- 2. Install the ZFS kernel module (bench-setup.md §2). The Firecracker
# vmlinux ships NO builtin ZFS and NO /lib/modules, so `zfsutils-linux`
# (userspace) alone leaves `modprobe zfs` FATAL — which would abort init's ZFS
# volume step ([volume].enabled = true). DKMS-build zfs.ko against the running
# kernel (the matching linux-headers package IS in noble-updates and the vmlinux
# loads externally-built modules) and load it. This is the ONE ZFS thing build.sh
# does — bench-cli's VolumeManager handles the pool/datasets itself. ---
apt-get update
apt-get install -y --no-install-recommends \
	dkms zfsutils-linux zfs-dkms "linux-headers-$(uname -r)"
modprobe zfs

# --- 3. Create the bench user (bench-setup.md §3): uid 1000, empty password,
# passwordless sudo. install.sh and `bench` run as this user; its lingering
# systemd --user units are what make the golden boot serving. Idempotent. ---
if ! id -u "$BENCH_USER" >/dev/null 2>&1; then
	useradd -ms /bin/bash -u 1000 -U -p '' "$BENCH_USER"
fi
usermod -aG sudo "$BENCH_USER"
echo "$BENCH_USER ALL=(ALL) NOPASSWD: ALL" >/etc/sudoers.d/"$BENCH_USER"
chmod 0440 /etc/sudoers.d/"$BENCH_USER"

# --- 4. Install bench-cli (bench-setup.md §4). install.sh clones bench-cli to
# ~/bench-cli, installs uv + Node, adds bench-cli to PATH in ~/.bashrc, sets up
# the .admin-venv (flask/psutil/pymysql), and — the NOPASSWD sudoers file already
# exists, so its own sudoers/Node steps run non-interactively. We then check out
# the pinned ref so the golden is reproducible (install.sh tracks moving main).
#
# Idempotent: run install.sh only on a FRESH guest (no bench-cli dir yet). A
# re-run must NOT re-invoke install.sh: it `git pull`s to self-update, which
# FATALs on the detached HEAD the pin below leaves behind ("You are not currently
# on a branch"). Re-running just re-fetches + re-pins the ref, which is all the
# golden's reproducibility needs. ---
if [ ! -d "$BENCH_CLI_DIR/.git" ]; then
	as_frappe "curl -fsSL https://raw.githubusercontent.com/frappe/bench-cli/$BENCH_CLI_REF/install.sh | bash"
fi
as_frappe "git -C '$BENCH_CLI_DIR' fetch --quiet origin && git -C '$BENCH_CLI_DIR' checkout --quiet '$BENCH_CLI_REF'"

# --- 5. Create the bench + drop our pinned bench.toml (bench-setup.md §5).
# `bench new` scaffolds benches/<name>/ non-interactively (name positional, no
# prompts); we overwrite its generated bench.toml with the committed one so the
# image's config is ours, not bench-cli's template. Idempotent: skip `bench new`
# if the bench dir already exists; the toml copy is an overwrite either way. ---
if [ ! -f "$BENCH_DIR/bench.toml" ]; then
	as_frappe "bench new '$BENCH_NAME'"
fi
install -m 0644 -o "$BENCH_USER" -g "$BENCH_USER" "$SRC_DIR/bench.toml" "$BENCH_DIR/bench.toml"

# --- 6. `bench init` (bench-setup.md §6). The heavy, idempotent step that sets
# up EVERYTHING from bench.toml: the ZFS pool + datasets (volume.enabled), the
# DEDICATED mariadb@atlas instance (provisioned, secured, enabled-at-boot) + Redis,
# the uv venv, the Frappe clone, Node deps, the admin frontend, and — because
# [production].nginx = true + process_manager = "systemd" — the production process
# units + nginx config + dns_multitenant = 1. We `source .admin-venv/bin/activate`
# first so bench-cli finds pymysql (it lives in the admin venv, not system python),
# exactly as bench-setup.md does. ---
as_frappe "source '$ADMIN_VENV/bin/activate' && bench -b '$BENCH_NAME' init"

# --- 7. Site mode only: bake a fully-created Frappe + ERPNext site, taking the
# heaviest per-signup costs (`bench new-site` + `install-app erpnext`) once here.
# admin mode bakes no site — the clone's domain maps to the admin app instead. ---
if [ "$MODE" = "site" ]; then
	# `get-app` clones ERPNext + builds its assets into the venv; it needs no
	# running bench. `new-site` only VALIDATES --apps (it does not install them),
	# so install-app erpnext is a separate, required step. install-app enqueues
	# background jobs, so Redis must be up: `bench start` brings the production
	# stack up (its systemd units), which we leave running for the rest of the bake.
	if [ ! -d "$BENCH_DIR/apps/erpnext" ]; then
		as_frappe "bench -b '$BENCH_NAME' get-app https://github.com/frappe/erpnext --branch version-16"
	fi

	as_frappe "bench -b '$BENCH_NAME' start"

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
	# beyond the stack being up.
	as_frappe "bench -b '$BENCH_NAME' start"
fi

# --- 8. Trim build cruft so golden copies are lean. The stack is LEFT RUNNING.
# The e2e re-asserts the bake over guest-SSH after the snapshot boots. ---
apt-get clean
rm -rf /var/lib/apt/lists/* "$BENCH_HOME/.cache" 2>/dev/null || true

echo "Golden bench image baked (mode=$MODE): bench-cli @ ${BENCH_CLI_REF:0:12}, bench '$BENCH_NAME'$([ "$MODE" = site ] && echo " + ERPNext site '$BAKED_SITE'"), production stack running."
