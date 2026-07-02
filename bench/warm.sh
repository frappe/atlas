#!/usr/bin/env bash
# Arm the golden bench VM for a WARM capture — run INSIDE the guest over
# guest-SSH by the Image Build warm finalize, AFTER build.sh, immediately BEFORE
# warm-snapshot-vm.py freezes it. build.sh already left the production stack UP
# and serving (its `bench start` under systemd-mode); a warm capture freezes that
# running stack, so whatever is resident in RAM at the pause is what every
# restored clone wakes into.
#
# This is deliberately small. The bench stack, nginx (with v6 listeners), and
# MariaDB are all bench-cli's job and already up — the ONLY per-clone deploy work
# on a warm resume is `mv site.local <fqdn>` + `bench setup nginx` (deploy-site.py).
# So warm.sh does just three things the freeze itself needs:
#
#   1. Install + start the identity freshen unit (atlas-warm-freshen.py): it must
#      be ALIVE mid-loop at the capture instant so every clone wakes with it
#      running and adopts its own identity from MMDS.
#   2. Pre-warm with REAL localhost HTTP (login + /app + pings) so gunicorn
#      workers, the MariaDB buffer pool, compiled assets, and bootinfo are
#      resident in the frozen RAM (and the asset cache lands on the captured disk).
#   3. Clone-entropy hygiene (delete the systemd random-seed) + a final `sync` so
#      everything written here is on the crash-consistent disk the cold-boot
#      FALLBACK uses, not only in the frozen page cache.
#
# Takes the build VM's uuid as $1 (written to /etc/atlas-vm-uuid: the freshen
# unit's "identity already adopted" marker — a clone whose MMDS uuid matches it
# does nothing, so the golden itself never self-freshens). Idempotent. Run as root
# (identity adoption is a root concern); the one bench command runs as frappe.

set -euo pipefail

VM_UUID="${1:?usage: warm.sh <virtual-machine-uuid>}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Kept in lockstep with build.sh / deploy-site.py.
BAKED_SITE="site.local"
BENCH_USER="frappe"
BENCH_HOME="/home/$BENCH_USER"
BENCH_CLI_DIR="$BENCH_HOME/bench-cli"
BENCH_NAME="atlas"
BENCH="$BENCH_CLI_DIR/bench"

# Run a bench-cli command as the bench user through a login shell — mirrors
# build.sh's as_frappe (same PATH/XDG_RUNTIME_DIR need for `systemctl --user`).
as_frappe() {
	sudo -u "$BENCH_USER" bash -lc "export PATH='$BENCH_CLI_DIR':\$PATH; export XDG_RUNTIME_DIR=/run/user/\$(id -u); cd '$BENCH_CLI_DIR'; $*"
}

# --- 1. The freshen unit. Restart=always: the loop must survive any crash — a
# clone restored from a golden whose freshen died could never be reached. ---
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
echo "$VM_UUID" >/etc/atlas-vm-uuid
systemctl daemon-reload
systemctl enable atlas-warm-freshen.service
systemctl restart atlas-warm-freshen.service

# --- 2. Pre-warm. Real requests through the full nginx → gunicorn → MariaDB path
# against the baked site.local (its vhost is what's frozen; the FQDN rename
# happens per clone at deploy). The Administrator login + /app GET walks the
# expensive desk bootinfo/asset path — the benchmark's single biggest
# first-request cost. build.sh now bakes a RANDOM admin password (never
# surfaced), so pre-warm no longer logs in with a password — it mints a session
# the same way deploy-site.py hands the tenant one, via `bench browse` (there is
# no `--sid` flag on stock Frappe's `browse` — it prints `Login URL: <url>?sid=
# <sid>`, so the sid is pulled out of that line instead). ---
warm_curl() {
	curl -s -o /dev/null -H "Host: $BAKED_SITE" "$@"
}
BROWSE_OUT="$(as_frappe "'$BENCH' -b '$BENCH_NAME' --site '$BAKED_SITE' browse --user Administrator")"
SID="$(grep -oP 'sid=\K\S+' <<<"$BROWSE_OUT")"
if [[ -z "$SID" ]]; then
	echo "pre-warm failed: bench browse did not print a Login URL with a sid: $BROWSE_OUT" >&2
	exit 1
fi
warm_curl -b "sid=$SID" http://127.0.0.1/app
warm_curl http://127.0.0.1/login
for _ in 1 2 3 4 5; do
	warm_curl http://127.0.0.1/api/method/ping
done

# The stack must actually be serving — this is what the frozen RAM answers the
# moment a clone resumes. Assert BOTH families: bench-cli now emits `listen
# [::]:80`, and the controller's readiness probe + the edge proxy's south hop
# arrive over v6, so a v4-only 200 would freeze a guest that fails every real probe.
for host_ip in 127.0.0.1 "[::1]"; do
	PING="$(curl -sg -H "Host: $BAKED_SITE" "http://$host_ip/api/method/ping")"
	if [[ "$PING" != *pong* ]]; then
		echo "pre-warm failed: ping via $host_ip returned: $PING" >&2
		exit 1
	fi
done

# --- 3. Clone-entropy hygiene, then FLUSH. The capture pairs the frozen RAM with
# a crash-consistent disk snapshot; everything written above (the freshen unit +
# its enable symlink, the deleted random-seed) may still be dirty in the page
# cache — present in the resumed RAM but ABSENT from the disk the cold-boot
# FALLBACK boots from. Proven on a real host: without this sync the fallback boots
# a guest with no freshen unit, never adopts its identity, and is unreachable. ---
rm -f /var/lib/systemd/random-seed
sync

echo "Warm bake armed: freshen unit live, production stack warm on '$BAKED_SITE', uuid $VM_UUID."
