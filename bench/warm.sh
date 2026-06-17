#!/usr/bin/env bash
# Arm the golden bench VM for a WARM capture — run INSIDE the guest over
# guest-SSH by the Image Build warm finalize, AFTER build.sh, immediately
# BEFORE warm-snapshot-vm.py freezes it. Where build.sh leaves the bench
# STOPPED (the cold golden's contract), the warm bake needs the opposite: the
# full production stack UP and genuinely WARM, because whatever is resident in
# RAM at the pause is what every restored clone wakes into.
#
#   1. Install + start the identity freshen unit (atlas-warm-freshen.py): it
#      must be ALIVE mid-loop at the capture instant so every clone wakes with
#      it running and adopts its own identity from MMDS.
#   2. `bench setup production` against the baked site.local — nginx +
#      supervisor up, the same bring-up deploy-site.py performs per site.
#   3. Pre-warm with REAL localhost HTTP (login + /app + /login + pings) so
#      gunicorn workers, the MariaDB buffer pool, compiled assets and bootinfo
#      are resident in the frozen RAM (and the asset cache lands on the
#      captured disk).
#   4. Delete the systemd random-seed (clone-entropy hygiene; the kernel CSPRNG
#      itself is reseeded per clone by Firecracker's VMGenID).
#
# Takes the build VM's uuid as $1 (written to /etc/atlas-vm-uuid: the freshen
# unit's "identity already adopted" marker — a clone whose MMDS uuid matches it
# does nothing, so the golden itself never self-freshens). Idempotent: re-runs
# overwrite the unit and re-warm. Run as root.

set -euo pipefail

VM_UUID="${1:?usage: warm.sh <virtual-machine-uuid>}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# bench-cli + uv on PATH (a non-interactive SSH command sources no profile).
source /etc/profile.d/atlas-bench.sh

# Kept in lockstep with build.sh / deploy-site.py.
BENCH_CLI_DIR="/root/bench-cli"
BENCH_NAME="atlas"
BAKED_SITE="site.local"

# --- 1. The freshen unit. Restart=always: the loop must survive any crash —
# a clone restored from a golden whose freshen died could never be reached. ---
install -m 0755 "$SRC_DIR/atlas-warm-freshen.py" /usr/local/bin/atlas-warm-freshen
install -m 0644 /dev/stdin /etc/systemd/system/atlas-warm-freshen.service <<'EOF'
[Unit]
Description=Atlas warm-clone identity freshen (MMDS poller)
After=atlas-network.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/atlas-warm-freshen
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
echo "$VM_UUID" > /etc/atlas-vm-uuid
systemctl daemon-reload
systemctl enable atlas-warm-freshen.service
systemctl restart atlas-warm-freshen.service

# --- 2. Production bring-up against the baked site. The warm clone resumes THIS
# running stack already serving site.local for any Host — so it is reachable the
# instant it resumes, BEFORE the per-clone deploy renames it to the FQDN + runs
# `bench setup nginx`. This is what must be frozen serving. ---
"$BENCH_CLI_DIR/bench" -b "$BENCH_NAME" setup production

# `bench setup production` emits vhosts with a bare `listen 80;` (IPv4 only) and
# `server_name site.local`. A restored clone is probed — and served — over its
# public /128 with `Host: <fqdn>`, so the frozen nginx needs BOTH: `listen [::]:80`
# (IPv6 is the only inbound path) AND `default_server` (the proxy's `<fqdn>` Host
# doesn't match `server_name site.local`, so the single block must be the catch-all
# that serves any Host → gunicorn → default_site = site.local). Same edit build.sh
# and deploy-site.py apply. Baked here so the FROZEN nginx already serves any Host
# on v4+v6 — the warm clone needs no nginx work at all. Idempotent.
BENCH_DIR="$BENCH_CLI_DIR/benches/$BENCH_NAME"
shopt -s nullglob
for conf in "$BENCH_DIR"/config/nginx/sites/*.conf "$BENCH_DIR"/config/nginx.conf; do
	[ -f "$conf" ] || continue
	if ! grep -q 'listen \[::\]:80' "$conf"; then
		sed -i 's/^\([[:space:]]*\)listen 80;/\1listen 80 default_server;\n\1listen [::]:80 default_server;/' "$conf"
	fi
done
shopt -u nullglob
systemctl reload nginx

# --- 3. Pre-warm. Real requests through the full nginx → gunicorn → MariaDB
# path. The Administrator login + /app GET walks the expensive desk
# bootinfo/asset path (the benchmark's single biggest first-request cost); the
# baked admin password is build.sh's throwaway, reset per clone at deploy. ---
warm_curl() {
	curl -s -o /dev/null -H "Host: $BAKED_SITE" "$@"
}
COOKIES=/tmp/atlas-warm-cookies
curl -s -o /dev/null -c "$COOKIES" -H "Host: $BAKED_SITE" \
	-d "usr=Administrator&pwd=$BENCH_NAME-baked" http://127.0.0.1/api/method/login
warm_curl -b "$COOKIES" http://127.0.0.1/app
warm_curl http://127.0.0.1/login
for _ in 1 2 3 4 5; do
	warm_curl http://127.0.0.1/api/method/ping
done
rm -f "$COOKIES"

# The stack must actually be serving — this is what the frozen RAM answers
# with the moment a clone resumes. Assert BOTH families: the controller's
# readiness probe (and the edge proxy's south hop) arrive over v6, so a
# v4-only 200 here would freeze a guest that fails every real probe.
for host_ip in 127.0.0.1 "[::1]"; do
	PING="$(curl -sg -H "Host: $BAKED_SITE" "http://$host_ip/api/method/ping")"
	if [[ "$PING" != *pong* ]]; then
		echo "pre-warm failed: ping via $host_ip returned: $PING" >&2
		exit 1
	fi
done

# --- 4. Clone-entropy hygiene. ---
rm -f /var/lib/systemd/random-seed

# --- 5. FLUSH. The capture that follows pairs the frozen RAM with a
# crash-consistent disk snapshot. Everything this script wrote (the freshen
# unit + its enable symlink, the v6 listeners, the deleted random-seed) may
# still be dirty in the page cache — present in the RAM every restored clone
# resumes with, but ABSENT from the disk the cold-boot FALLBACK boots from.
# Proven on a real host: without this sync the fallback boots a guest with no
# freshen unit, never adopts its identity, and is unreachable forever. ---
sync

echo "Warm bake armed: freshen unit live, production stack warm on '$BAKED_SITE', uuid $VM_UUID."
