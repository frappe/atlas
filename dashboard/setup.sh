#!/usr/bin/env bash
#
# setup.sh — one command to run the dashboard against real Atlas hosts.
#
# It (1) asks the bench for the Atlas SSH key + the real Server addresses (using
# only built-in Frappe calls — nothing is deployed to the bench), (2) starts the
# dev SSH proxy (backend/proxy.py), which pushes server.py to each host over ssh
# and serves live /api/state, and (3) starts Vite pointed at the proxy and opens
# the dashboard. Nothing is installed on any host — the proxy runs the read-only
# collector from a pipe, reaching hosts exactly as Atlas Tasks do (root@<ipv4>
# with the Atlas key).
#
# Usage:
#   ./setup.sh                       # discover hosts from the bench, run all
#   ./setup.sh root@1.2.3.4 f2 ...   # skip discovery; use these ssh dests
#
# Env:
#   ATLAS_SITE   bench site to read Servers from   (default: scaleway.local)
#   BENCH_DIR    bench root                          (default: derived from tree)
#   PROXY_PORT   proxy port                          (default: 8080)
#   NO_VITE=1    just run the proxy (curl it, or serve a built dist/ yourself)
#   NO_OPEN=1    don't open the browser
#
set -euo pipefail

cd "$(dirname "$0")"                       # dashboard/
ATLAS_SITE="${ATLAS_SITE:-scaleway.local}"
PROXY_PORT="${PROXY_PORT:-8080}"
# Bench root is three levels up:  <bench>/trees/<tree>/dashboard -> <bench>
BENCH_DIR="${BENCH_DIR:-$(cd ../../.. && pwd)}"
MANIFEST="$(mktemp -t atlas-hosts.XXXXXX.json)"

say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Run a Frappe builtin headlessly and echo the last (JSON/value) line it prints.
bench_exec() { # <fn> <kwargs-json>
	(cd "$BENCH_DIR" && "$BENCH" --site "$ATLAS_SITE" execute "$1" --kwargs "$2" 2>/dev/null) \
		| tail -1
}

# ── discover real hosts from the bench (unless dests were passed) ────────────
DESTS=("$@")
if [ "${#DESTS[@]}" -eq 0 ]; then
	command -v python3 >/dev/null || die "python3 not found"
	BENCH="$BENCH_DIR/env/bin/bench"
	[ -x "$BENCH" ] || BENCH="$(command -v bench || true)"
	[ -n "$BENCH" ] || die "bench not found (set BENCH_DIR, or pass ssh hosts as arguments)"

	say "discovering Servers from $ATLAS_SITE …"
	SERVERS="$(bench_exec frappe.get_all \
		'{"doctype":"Server","fields":["title","ipv4_address","status"],"filters":{"ipv4_address":["is","set"]},"order_by":"title asc"}' || true)"
	case "$SERVERS" in
		\[*) : ;;  # looks like a JSON array — good
		*) die "discovery failed (site '$ATLAS_SITE' up? Server doctype present?). Got: ${SERVERS:-<nothing>}" ;;
	esac
	KEY="$(bench_exec frappe.db.get_single_value \
		'{"doctype":"Atlas Settings","fieldname":"ssh_private_key_path"}' || true)"

	# Turn the Server rows into the proxy's {id,label,dest} manifest.
	python3 - "$SERVERS" "$MANIFEST" <<'PY' || die "could not parse Servers (got: $SERVERS)"
import json, os, re, sys
rows = json.loads(sys.argv[1])
def slug(t): return (re.sub(r"[^a-z0-9._-]+","-",(t or "").lower()).strip("-") or "host")
seen, hosts = set(), []
for r in rows:
    ip = (r.get("ipv4_address") or "").strip()
    if not ip: continue
    hid = slug(r.get("title") or ip)
    while hid in seen: hid += "-x"
    seen.add(hid)
    hosts.append({"id": hid, "label": f"{r.get('title') or ip} · {r.get('status') or '?'}", "dest": f"root@{ip}"})
json.dump({"hosts": hosts}, open(sys.argv[2], "w"))
print(len(hosts))
PY
	COUNT="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["hosts"]))' "$MANIFEST")"
	[ "$COUNT" != "0" ] || die "no Servers with an ipv4 in $ATLAS_SITE"

	# Authenticate as Atlas does: its key + its known_hosts.
	[ -n "${KEY:-}" ] && export ATLAS_PROXY_SSH_KEY="${KEY/#\~/$HOME}"
	export ATLAS_PROXY_KNOWN_HOSTS="$HOME/.atlas/known_hosts"
	say "found $COUNT host(s); ssh key: ${ATLAS_PROXY_SSH_KEY:-<ssh default>}"
	PROXY_ARGS=(--hosts-json "$MANIFEST")
else
	say "using ${#DESTS[@]} host(s) from the command line"
	PROXY_ARGS=("${DESTS[@]}")
fi

# ── boot the proxy ──────────────────────────────────────────────────────────
export ATLAS_PROXY_PORT="$PROXY_PORT"
say "starting proxy on http://127.0.0.1:$PROXY_PORT"
python3 backend/proxy.py "${PROXY_ARGS[@]}" &
PIDS=("$!")
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; rm -f "$MANIFEST"; }
trap cleanup EXIT INT TERM

sleep 1
curl -fsS "http://127.0.0.1:$PROXY_PORT/api/state/sources" >/dev/null 2>&1 \
	|| die "proxy did not come up on :$PROXY_PORT (see its output above)"
say "proxy up. sources: $(curl -fsS http://127.0.0.1:$PROXY_PORT/api/state/sources)"

# ── boot Vite (unless NO_VITE) ──────────────────────────────────────────────
URL="http://127.0.0.1:$PROXY_PORT"
if [ -z "${NO_VITE:-}" ]; then
	[ -d node_modules ] || { say "installing npm deps …"; npm install; }
	say "starting Vite (VITE_API_BASE=$URL) …"
	VITE_API_BASE="$URL" npm run dev &
	PIDS+=("$!")
	URL="http://127.0.0.1:5173"
	sleep 2
fi

# ── open the browser ────────────────────────────────────────────────────────
if [ -z "${NO_OPEN:-}" ]; then
	{ command -v open >/dev/null && open "$URL"; } \
		|| { command -v xdg-open >/dev/null && xdg-open "$URL"; } || true
fi

say "dashboard: $URL"
say "raw state:  curl -s 'http://127.0.0.1:$PROXY_PORT/api/state?src=<id>' | jq ."
say "Ctrl-C to stop."
wait
