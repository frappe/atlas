#!/usr/bin/env bash
# Bake the golden bench image — run INSIDE a freshly-provisioned Ubuntu guest
# (spec/08-images.md § golden bench image). Installs bench-cli, runs `bench init`
# (the heavy, per-site-invariant work: apt MariaDB + Redis, the mandatory ZFS
# pool, the uv venv, the Frappe clone, Node + npm deps, the admin frontend),
# installs ERPNext, bakes a `site.local` site, runs `bench setup production`, and
# leaves the whole stack RUNNING and SERVING. The built VM is then snapshotted by
# Atlas; that snapshot is the reusable "golden bench image" `deploy-site.py`
# lands on — a snapshot-booted clone comes up serving the baked site, so
# deploy-site.py does only the per-VM rename (site.local → FQDN) + `bench setup
# nginx`; no admin reset (the baked password is the shared throwaway).
#
# This mirrors proxy/build.sh: the AUTHORITATIVE build, uploaded verbatim and run
# over guest-SSH by atlas.atlas.bench_image.build_bench. Idempotent (spec taste
# #16: retry = re-run) — bench-cli's `init` is itself idempotent, re-cloning
# bench-cli is a `git pull`, and every step below skips when its output exists.
#
# Bakes a SITE under the fixed standard name `site.local`, with ERPNext installed.
# The slow per-signup steps — `bench new-site` (DB schema + frappe install) and
# `install-app erpnext` (the heaviest) — are paid ONCE here. deploy-site.py
# RENAMES the baked site to the per-VM FQDN at deploy time (a directory move),
# moving that cost off the signup path entirely. The routing identity (Contract
# A) is per-VM — the rename target, applied per clone, not baked.
#
# Why this image SERVES on boot (not "leaves the bench stopped"): MariaDB + Redis
# are enabled, and a systemd boot unit (atlas-bench.service, written below) brings
# the bench-owned supervisord up after the ZFS mount + MariaDB. bench-cli's own
# supervisord is NOT a systemd service (it is started by hand by `bench start`),
# so without this unit a snapshot-booted clone would boot with nothing serving.
#
# Run as root. Reads the committed tree from the directory this script lives in.

set -euo pipefail

# --- Pinned versions. Bumping any of these is a deliberate image update rolled
# as a new golden snapshot (the same discipline proxy/build.sh's pins follow).
# bench-cli is pinned to a commit, not `main`, so the bake is reproducible; the
# Frappe branch (and the mandatory-ZFS / production schema bench.toml uses) is
# pinned in bench.toml. ---
BENCH_CLI_REPO="https://github.com/frappe/bench-cli"
BENCH_CLI_REF="f5274279f86b29db6c3f2f6bb4d7776690daa0c2"  # main @ 2026-06-11

BENCH_CLI_DIR="/root/bench-cli"
BENCH_NAME="atlas"
# The baked site. A clone of this image already carries a fully-created Frappe +
# ERPNext site under this name; deploy-site.py renames it to the per-VM FQDN at
# deploy time (a directory move, not a `bench new-site`) — see that script and
# the README "Serving model". Kept in lockstep with bench/deploy-site.py's
# BAKED_SITE and warm.sh's BAKED_SITE.
BAKED_SITE="site.local"
# The baked Administrator password — a SHARED throwaway, the SAME on every clone.
# deploy-site.py no longer resets it per VM (that cost a ~28s CPU-throttled `bench
# frappe` boot that dominated the deploy); the owner is handed this and rotates it
# after first login. warm.sh logs in with it to pre-warm the desk before the warm
# snapshot freezes. Kept in lockstep with warm.sh ("$BENCH_NAME-baked") AND with
# the controller's Site.BAKED_ADMIN_PASSWORD (which hands it to the owner).
BAKED_ADMIN_PASSWORD="$BENCH_NAME-baked"
BENCH_DIR="$BENCH_CLI_DIR/benches/$BENCH_NAME"
BENCH="$BENCH_CLI_DIR/bench"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
# We are root, so every `sudo` bench-cli shells out to is a passthrough. Setting
# IS_SUDOERS_SETUP makes `bench init` skip the passwordless-sudo step entirely —
# without it the current bench-cli ALWAYS expects a sudo password (the build is
# non-interactive, so a prompt would hang it). See the README "Passwordless sudo".
export IS_SUDOERS_SETUP=1

# --- 1. Base packages. bench-cli installs MariaDB + Redis + build-essential
# itself during `init`, but the bits it does NOT install — and which init would
# otherwise fail on — go here up front:
#   * zfsutils-linux: `bench init` now sets up a MANDATORY ZFS pool on Linux
#     (the root-disk-zfs merge). bench-cli auto-installs zfsutils if missing, but
#     we pre-install so the tools are present before the volume step.
#   * nginx + supervisor: the production front door. `[production].nginx = true`
#     in bench.toml makes `init` configure them; pre-installing avoids an apt
#     round-trip mid-init and lets us drop the stock default vhost below.
#   * git/curl/ca-certificates: the clone + uv fetch on a minimal rootfs.
#   * dbus / acl: harmless on the cloud image; present for parity. ---
apt-get update
apt-get install -y --no-install-recommends \
	ca-certificates curl git build-essential pkg-config \
	zfsutils-linux nginx supervisor

# The Atlas guest boots the Firecracker `vmlinux`, which ships NO /lib/modules
# tree and NO builtin ZFS — so `zfsutils-linux` (userspace only) leaves
# `modprobe zfs` FATAL ("Module zfs not found"), which aborts the ZFS-mandatory
# `bench init`. PROVEN on a real host: the matching kernel-headers package IS in
# noble-updates and the running vmlinux loads externally-built modules, so build
# zfs.ko with DKMS against the running kernel, then load it. Install the EXACT
# linux-headers-$(uname -r) (not the linux-headers-generic meta, which drags a
# newer ABI and DKMS-builds a second unused copy). `zfs-dkms` runs depmod +
# builds into /lib/modules/$(uname -r)/updates/dkms/ on install. Idempotent:
# already-built is a no-op; `modprobe zfs` is the gate `bench init` needs.
apt-get install -y --no-install-recommends \
	"linux-headers-$(uname -r)" zfs-dkms
modprobe zfs

# --- 2. Install bench-cli at the pinned commit (the install.sh recipe, but
# pinned — never `curl | bash` of a moving main at boot). Clone-or-update so a
# re-run is a fast-forward, then check out the exact ref. uv is installed to a
# SYSTEM path (/usr/local/bin), not just /root/.local/bin: bench-cli's
# admin_env_manager resolves uv with shutil.which(), which misses ~/.local/bin on
# the non-interactive PATH an SSH command gets. ---
if [ -d "$BENCH_CLI_DIR/.git" ]; then
	git -C "$BENCH_CLI_DIR" fetch --quiet origin
else
	git clone --quiet "$BENCH_CLI_REPO" "$BENCH_CLI_DIR"
fi
git -C "$BENCH_CLI_DIR" checkout --quiet "$BENCH_CLI_REF"
chmod +x "$BENCH"

if ! command -v uv >/dev/null 2>&1; then
	curl -LsSf https://astral.sh/uv/install.sh | sh
fi
for b in uv uvx; do
	[ -x /root/.local/bin/$b ] && install -m 0755 /root/.local/bin/$b /usr/local/bin/$b
done
export PATH="$BENCH_CLI_DIR:/usr/local/bin:/root/.local/bin:$PATH"

# Persist PATH for every future login shell (deploy-site.py / warm.sh reach bench
# over a fresh SSH session, which sources /etc/profile.d). Idempotent: overwrite.
install -m 0644 /dev/stdin /etc/profile.d/atlas-bench.sh <<EOF
export PATH="$BENCH_CLI_DIR:/usr/local/bin:/root/.local/bin:\$PATH"
EOF

# --- 3. Create the bench from the committed bench.toml (pins Frappe + the
# localhost-only MariaDB root password + the mandatory ZFS volume + the
# supervisor/nginx production config — see bench.toml). `bench new` scaffolds
# benches/<name>/; we drop our pinned bench.toml over the generated one so the
# image's config is the committed one, not bench-cli's template defaults.
#
# `bench new` at this pinned commit INTERACTIVELY prompts for the default branch
# (version-16/develop) via input(); a non-interactive build has no stdin, so the
# read hits EOF and aborts ("EOF when reading a line"). Feed one newline so the
# prompt takes its [1] default (version-16). The value is moot anyway — we
# overwrite the generated bench.toml with the committed one on the next line, so
# the branch pin that actually matters is bench.toml's, not this prompt's. ---
if [ ! -f "$BENCH_DIR/bench.toml" ]; then
	printf '\n' | "$BENCH" new "$BENCH_NAME"
fi
install -m 0644 "$SRC_DIR/bench.toml" "$BENCH_DIR/bench.toml"

# `bench init` is the heavy, idempotent step. Because bench.toml sets
# `[production].nginx = true`, init also sets up the supervisor process group +
# the nginx config (with no site vhost yet — `setup production` regenerates that
# in §7 once the site exists). It greps its own success line.
"$BENCH" -b "$BENCH_NAME" init 2>&1 | tee /root/bench-init.log
grep -q "Bench initialised" /root/bench-init.log

# --- 4. MariaDB root → password auth. Stock Ubuntu MariaDB authenticates root
# via the unix_socket plugin; Frappe connects to the DB with the bench.toml
# PASSWORD (over TCP), which the socket plugin rejects (error 1698). Switch root
# to mysql_native_password with the baked secret so `new-site` + `install-app`
# can authenticate. Idempotent: ALTER USER is declarative.
#
# init's volume setup STOPS MariaDB, moves /var/lib/mysql onto the ZFS dataset,
# and RESTARTS it — but that restart is fire-and-forget (bench-cli swallows a
# failed start), and init's "Bench initialised" prints before the DB is
# necessarily accepting connections. So gate on a real socket ping first: fail
# loud if MariaDB never comes up (a stopped DB here would hang or fail the ALTER
# and ship a broken golden), then run the ALTER once it answers. ---
for _ in $(seq 1 60); do
	mysqladmin --protocol=socket ping >/dev/null 2>&1 && break
	sleep 0.5
done
mysqladmin --protocol=socket ping >/dev/null 2>&1 || { echo "MariaDB not accepting connections after bench init" >&2; exit 1; }
mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('atlas'); FLUSH PRIVILEGES;"

# --- 5. Install ERPNext (version-16) into the bench. `get-app` clones + uv-pip
# installs it into the venv and builds assets; it does NOT need Redis or a
# running bench. Idempotent: skip if already cloned. ---
if [ ! -d "$BENCH_DIR/apps/erpnext" ]; then
	"$BENCH" -b "$BENCH_NAME" get-app https://github.com/frappe/erpnext --branch version-16
fi

# --- 6. Bake the site. `bench new-site` creates the MariaDB schema + installs
# frappe; `install-app erpnext` (the heaviest per-signup step) installs the
# ERPNext schema. new-site's `--apps` only VALIDATES the app is present — it does
# NOT install it — so install-app is a separate, required step. install-app
# enqueues background jobs, so Redis must be running: start the bench-owned
# supervisord (which runs redis) first. Idempotent: skip if the site exists. ---
"$BENCH" -b "$BENCH_NAME" start >/root/bench-start.log 2>&1 &
# Wait for redis (the queue install-app enqueues onto) to come up.
for _ in $(seq 1 30); do
	redis-cli -p 13000 ping >/dev/null 2>&1 && break
	sleep 1
done

if [ ! -d "$BENCH_DIR/sites/$BAKED_SITE" ]; then
	"$BENCH" -b "$BENCH_NAME" new-site "$BAKED_SITE" \
		--admin-password "$BAKED_ADMIN_PASSWORD" --apps erpnext
	"$BENCH" -b "$BENCH_NAME" frappe --site "$BAKED_SITE" install-app erpnext
	"$BENCH" -b "$BENCH_NAME" frappe --site "$BAKED_SITE" migrate
fi

# Take the baked site PAST the setup-wizard gate so a renamed clone serves the
# app at `/`, not a redirect to /setup-wizard (memory: fresh-site-setup-gate).
# The real gate is `Installed Application.is_setup_complete` for the frappe row
# (NOT just System Settings); set both. `bench frappe … execute` auto-commits.
# Baked here so deploy-site.py's rename path stays a pure move + password reset.
"$BENCH" -b "$BENCH_NAME" frappe --site "$BAKED_SITE" execute \
	frappe.db.set_value \
	--args '["Installed Application", {"app_name": "frappe"}, "is_setup_complete", 1]'
"$BENCH" -b "$BENCH_NAME" frappe --site "$BAKED_SITE" execute \
	frappe.db.set_single_value --args '["System Settings", "setup_complete", 1]'

# --- 7. Production bring-up. `bench build` downloads/builds the site assets;
# `bench setup production` flips dns_multitenant on, regenerates the nginx config
# WITH the site vhost, and reloads the supervisor group. Idempotent + whole-bench
# (not per-site), so deploy-site.py / warm.sh re-run it safely per clone. No
# letsencrypt.email is set, so it never attempts certbot — TLS terminates at the
# edge proxy (spec/14-self-serve.md). ---
"$BENCH" -b "$BENCH_NAME" build
"$BENCH" -b "$BENCH_NAME" setup production

# `bench setup production` emits the site vhost with a bare `listen 80;` (IPv4
# only) and `server_name site.local`. Two edits, both load-bearing on the
# no-rename serving model (spec/14-self-serve.md):
#   * `listen [::]:80` — the EDGE proxy reaches each site over the VM's public
#     /128; IPv6 is the only inbound path (vm-inbound-ipv6-only). Without a v6
#     listener the vhost never matches a v6 request (dead on the path that matters).
#   * `default_server` — the site stays on disk as `site.local`, but the proxy
#     forwards `Host: <fqdn>`, which does NOT match `server_name site.local`.
#     Marking the (single) block `default_server` makes nginx serve it for ANY
#     unmatched Host, so `<fqdn>` is handled and proxied to gunicorn (which serves
#     `default_site = site.local` regardless of Host). Baking it means the golden's
#     frozen/booted nginx already serves any Host on v4+v6 — so a WARM clone needs
#     NO per-clone nginx step at all. Same edit deploy-site.py (_add_ipv6_listen)
#     applies on the cold path. Idempotent — the presence check skips a re-add.
add_ipv6_listeners() {
	shopt -s nullglob
	for conf in "$BENCH_DIR"/config/nginx/sites/*.conf "$BENCH_DIR"/config/nginx.conf; do
		[ -f "$conf" ] || continue
		grep -q 'listen \[::\]:80' "$conf" && continue
		sed -i 's/^\([[:space:]]*\)listen 80;/\1listen 80 default_server;\n\1listen [::]:80 default_server;/' "$conf"
	done
	shopt -u nullglob
}
add_ipv6_listeners

# --- 8. Cold-boot bring-up. Everything the bench needs (the bench code AND the
# MariaDB data) lives on ZFS datasets — `bench init` mounts bench-pool/benches at
# /root/bench-cli/benches and bench-pool/mariadb at /var/lib/mysql. So on a cold
# boot of the snapshot, the pool must auto-import + mount BEFORE MariaDB and the
# bench start, or both come up against empty dirs and die.

# 8a. ZFS auto-import at boot from the cachefile (a file-backed pool isn't
# auto-discovered by a device scan). Ensure zfs.ko loads early on every boot.
echo zfs > /etc/modules-load.d/zfs.conf
zpool set cachefile=/etc/zfs/zpool.cache bench-pool
systemctl enable zfs-import-cache.service zfs-mount.service zfs.target zfs-import.target 2>/dev/null || true
systemctl disable --now zfs-import-scan.service 2>/dev/null || true

# 8b. MariaDB + nginx must wait for the ZFS mount (their data/config live on it).
# Order on the concrete zfs-mount.service, not zfs.target (which can hang
# "activating" if zed is half-disabled, silently starving anything After=it).
install -d /etc/systemd/system/mariadb.service.d /etc/systemd/system/nginx.service.d
cat > /etc/systemd/system/mariadb.service.d/10-zfs.conf <<'EOF'
[Unit]
After=zfs-mount.service
Wants=zfs-mount.service
EOF
cat > /etc/systemd/system/nginx.service.d/10-zfs.conf <<'EOF'
[Unit]
After=zfs-mount.service
Wants=zfs-mount.service
[Service]
Restart=on-failure
RestartSec=1
EOF

# 8c. The bench-owned supervisord as a systemd boot unit. bench-cli's supervisor
# manager expects `bench start` to launch supervisord by hand; for an unattended
# boot we run it as a `system` unit, after the ZFS mount + MariaDB, and wait in
# ExecStartPre until both the benches mount and MariaDB's socket are actually up
# (the mount/DB can lose the ordering race on a busy boot). Everything runs as
# root in this image, so no User= / linger dance. supervisord -n stays in the
# foreground so systemd supervises it directly.
SUPERVISORD_DIR="$BENCH_DIR/config/supervisor"
SUPERVISORD_CONF="$SUPERVISORD_DIR/supervisord.conf"
install -m 0644 /dev/stdin /etc/systemd/system/atlas-bench.service <<EOF
[Unit]
Description=Atlas bench (bench-cli supervisord)
After=zfs-mount.service mariadb.service network-online.target
Wants=zfs-mount.service mariadb.service
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$BENCH_DIR
ExecStartPre=/bin/sh -c 'until mountpoint -q $BENCH_CLI_DIR/benches && mysqladmin --protocol=socket ping >/dev/null 2>&1; do sleep 0.1; done'
ExecStart=/usr/bin/supervisord -n -c $SUPERVISORD_CONF
Restart=on-failure
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable mariadb.service 2>/dev/null || systemctl enable mysql.service 2>/dev/null || true
systemctl enable redis-server.service 2>/dev/null || true
systemctl enable nginx.service 2>/dev/null || true
systemctl enable atlas-bench.service 2>/dev/null || true

# Remove the stock Ubuntu default nginx vhost. It listens `[::]:80 default_server`
# (server_name _), so it OWNS the IPv6 :80 socket and answers 404 to every v6
# request that doesn't match a named vhost — and the edge proxy reaches each site
# over its public /128 (IPv6 is the only inbound path). Left in place it silently
# shadows the real site on the v6 path while v4 looks fine. Idempotent (`-f`).
rm -f /etc/nginx/sites-enabled/default

# --- 9. Make the running stack serve, and assert it. The bench-owned supervisord
# was started by hand in §6 (for install-app); hand it over to the systemd unit so
# the same supervisord that serves at runtime is the one systemd supervises (and
# that boots a cold clone). `bench stop` issues `supervisorctl shutdown`, which
# returns BEFORE supervisord has fully exited and released its pidfile + unix
# socket — so the systemd `supervisord -n` that follows would collide on a stale
# socket/pidfile. Wait for the pidfile to vanish, then sweep any leftover socket,
# before starting the unit. Then reload nginx for the v6 listeners, and prove the
# site answers on BOTH families — the readiness probe and the edge proxy's south
# hop arrive over v6, so a v4-only 200 would ship a golden that fails every real
# probe. ---
"$BENCH" -b "$BENCH_NAME" stop >/dev/null 2>&1 || true
for _ in $(seq 1 30); do
	[ -f "$SUPERVISORD_DIR/supervisord.pid" ] || break
	sleep 0.5
done
rm -f "$SUPERVISORD_DIR/supervisord.pid" "$SUPERVISORD_DIR/supervisord.sock"
systemctl restart atlas-bench.service
systemctl reload nginx 2>/dev/null || systemctl restart nginx

for _ in $(seq 1 60); do
	curl -sf -o /dev/null -H "Host: $BAKED_SITE" http://127.0.0.1/api/method/ping && break
	sleep 1
done
for host_ip in 127.0.0.1 "[::1]"; do
	ping_body="$(curl -sg -m 10 -H "Host: $BAKED_SITE" "http://$host_ip/api/method/ping" || true)"
	if [[ "$ping_body" != *pong* ]]; then
		echo "serve check FAILED: ping via $host_ip returned: $ping_body" >&2
		exit 1
	fi
done

# --- 10. Trim build cruft so golden copies are lean, then assert the bake
# produced a working bench (frappe + erpnext installed). The e2e re-asserts it
# over guest-SSH after the snapshot boots. The stack is LEFT RUNNING + SERVING. ---
apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache 2>/dev/null || true
"$BENCH" -b "$BENCH_NAME" list-site-apps "$BAKED_SITE"

echo "Golden bench image baked: bench-cli @ ${BENCH_CLI_REF:0:12}, bench '${BENCH_NAME}' with ERPNext + baked site '${BAKED_SITE}', production stack running and serving on v4 + v6."
