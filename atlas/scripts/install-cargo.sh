#!/bin/bash
# Bring up a production Cargo host from a bare Ubuntu machine.
#
# Pilot's installer brings its own MariaDB, Redis and nginx, so nothing is expected to be
# on the machine beforehand. Everything below the passwords is what the app is installed
# with: the provisioner knows all of it, and Cargo Settings has no other way to learn it.
#
# The bench is deployed to production behind nginx on port 80, without TLS: HTTPS is
# terminated by the proxy in front of this host.
set -euo pipefail

BENCH="${BENCH:-cargo}"
SITE="${SITE:-cargo.localhost}"
ADMIN_DOMAIN="${ADMIN_DOMAIN:-}"
BRANCH="${BRANCH:-develop}"
REPO="${REPO:-https://github.com/frappe/cargo}"
# Two different passwords. MariaDB's root password is not one of them: pilot generates
# that itself when it initialises the bench.
PILOT_ADMIN_PASSWORD="${PILOT_ADMIN_PASSWORD:-}"   # pilot's own admin panel
SITE_PASSWORD="${SITE_PASSWORD:-}"     # the site's Frappe Administrator
# One per mandatory field of Cargo Settings, and nothing else: the install hook writes
# these straight onto it, so anything missing here fails the install.
CENTRAL_URL="${CENTRAL_URL:-}" # Central's URL for this host to call
JWKS_URL="${JWKS_URL:-}" # JWKS endpoint
ATLAS_URL="${ATLAS_URL:-}" # Atlas's URL for this host to call
CARGO_URL="${CARGO_URL:-}" # where this host answers
CENTRAL_WEBHOOK_SECRET="${CENTRAL_WEBHOOK_SECRET:-}" # signs the reports this host sends Central
REGION="${REGION:-}" # which region this Cargo provisions for
REGION_ID="${REGION_ID:-}" # that region's numeric id, as Atlas knows it
ATLAS_TOKEN="${ATLAS_TOKEN:-}"
ATLAS_TENANT_ID="${ATLAS_TENANT_ID:-}" # the tenant every Atlas call of this host is scoped to
PROXY_URL="${PROXY_URL:-}" # Proxy control API URL for this region
PROXY_TOKEN="${PROXY_TOKEN:-}" # restricted token for the Proxy control API
WILDCARD_DOMAIN="${WILDCARD_DOMAIN:-}"
# Site config, not a Cargo Settings field: Cargo reads it to build its storage cluster on
# first boot. JSON with storage_node_count, replication_factor, and a gateway and storage
# block of cpu, ram_gb and disk_gb.
DEFAULT_STORAGE_CLUSTER_CONFIG="${DEFAULT_STORAGE_CLUSTER_CONFIG:-}"
# Site config, not a Cargo Settings field: Cargo reads it to build its Datum host on first
# boot. JSON with repository, branch, and a telemetry block of cpu_millicores, ram_gb and disk_gb.
DEFAULT_TELEMETRY_CONFIG="${DEFAULT_TELEMETRY_CONFIG:-}"
BENCH_USER="${BENCH_USER:-frappe}" # pilot refuses to run as root, so the bench gets its own user
BENCH_UID="${BENCH_UID:-1000}"
BENCH_GID="${BENCH_GID:-1000}"

if [ -z "$PILOT_ADMIN_PASSWORD" ] || [ -z "$SITE_PASSWORD" ]; then
	echo "Set PILOT_ADMIN_PASSWORD and SITE_PASSWORD before running." >&2
	exit 1
fi

# Pilot's own rule, checked here rather than by `pilot new` -- which runs after the whole
# system stack is built, so a weak password otherwise costs ten minutes to find out about.
weak_password() {
	case "${#1}" in 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7) echo "at least 8 characters"; return ;; esac
	case "$1" in *[a-z]*) ;; *) echo "a lower case letter"; return ;; esac
	case "$1" in *[A-Z]*) ;; *) echo "an upper case letter"; return ;; esac
	case "$1" in *[0-9]*) ;; *) echo "a number"; return ;; esac
	case "$1" in *[!A-Za-z0-9]*) ;; *) echo "a symbol"; return ;; esac
}

for name in PILOT_ADMIN_PASSWORD SITE_PASSWORD; do
	if missing_part=$(weak_password "${!name}") && [ -n "$missing_part" ]; then
		echo "$name needs $missing_part." >&2
		echo "Pilot wants at least 8 characters, upper and lower case, a number and a symbol." >&2
		exit 1
	fi
done

if [ -z "$ADMIN_DOMAIN" ]; then
	echo "Set ADMIN_DOMAIN before running: production needs a domain for pilot's admin panel." >&2
	exit 1
fi

ENROLMENT_VARS="CENTRAL_URL JWKS_URL ATLAS_URL CARGO_URL CENTRAL_WEBHOOK_SECRET REGION REGION_ID \
	ATLAS_TOKEN ATLAS_TENANT_ID PROXY_URL PROXY_TOKEN WILDCARD_DOMAIN"

missing=""
for name in $ENROLMENT_VARS DEFAULT_STORAGE_CLUSTER_CONFIG DEFAULT_TELEMETRY_CONFIG; do
	[ -n "${!name}" ] || missing="$missing $name"
done

if [ -n "$missing" ]; then
	echo "Set$missing before running." >&2
	echo "The webhook secret and the region's id come from this host's Cargo Instance in Central." >&2
	exit 1
fi

# This script is run as root. Everything pilot does runs as the bench user instead:
# it refuses to run as root, and the bench's files must belong to whoever serves them.
if ! id -u "$BENCH_USER" > /dev/null 2>&1; then
	groupadd -g "$BENCH_GID" "$BENCH_USER" 2> /dev/null || groupadd "$BENCH_USER"
	useradd -m -s /bin/bash -u "$BENCH_UID" -g "$BENCH_USER" "$BENCH_USER" 2> /dev/null ||
		useradd -m -s /bin/bash -g "$BENCH_USER" "$BENCH_USER"
fi

as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

INSTALLER="https://raw.githubusercontent.com/frappe/pilot/develop/install.sh"

q_installer=$(printf '%q' "$INSTALLER")
q_bench=$(printf '%q' "$BENCH")
q_site=$(printf '%q' "$SITE")
q_repo=$(printf '%q' "$REPO")
q_branch=$(printf '%q' "$BRANCH")
q_admin_password=$(printf '%q' "$PILOT_ADMIN_PASSWORD")
q_site_password=$(printf '%q' "$SITE_PASSWORD")
q_admin_domain=$(printf '%q' "$ADMIN_DOMAIN")
q_cluster_config=$(printf '%q' "$DEFAULT_STORAGE_CLUSTER_CONFIG")
q_telemetry_config=$(printf '%q' "$DEFAULT_TELEMETRY_CONFIG")
# The install hook reads these, so they are quoted once and exported into that one command.
enrolment=""
for name in $ENROLMENT_VARS; do
	enrolment="$enrolment $name=$(printf '%q' "${!name}")"
done

# Twice: the root pass lays down the system stack and grants the bench user what it
# needs -- lingering, nginx and sudoers -- then stops. The second pass installs pilot
# for that user, needing no privileges.
curl -fsSL "$INSTALLER" | bash -s -- --user "$BENCH_USER"
as_bench_user "curl -fsSL $q_installer | bash"

# `new` only writes bench.toml. `init` is what builds the bench: virtualenv, framework,
# Node and Redis. Without it there is nothing for a site to be created in.
as_bench_user "pilot --yes new $q_bench --database mariadb --admin-password $q_admin_password"
as_bench_user "pilot --yes -b $q_bench init --no-dev"
as_bench_user "pilot --yes -b $q_bench new-site $q_site --admin-password $q_site_password"

# A new site pauses the scheduler, and Cargo needs its scheduled jobs to run often.
# `frappe` must be the first argument after the bench: pilot forwards the rest to Frappe
# verbatim only then, and its own argparse rejects `--site` otherwise.
as_bench_user "pilot -b $q_bench frappe set-config -g -p scheduler_tick_interval 5"
as_bench_user "pilot -b $q_bench frappe --site $q_site enable-scheduler"
# `-p` keeps the value JSON. Stored as a string, Cargo's spawner refuses it.
as_bench_user "pilot -b $q_bench frappe --site $q_site set-config -p default_storage_cluster_config $q_cluster_config"
as_bench_user "pilot -b $q_bench frappe --site $q_site set-config -p default_telemetry_config $q_telemetry_config"
as_bench_user "pilot --yes -b $q_bench get-app $q_repo --branch $q_branch --install-dependencies"

# Production before the app: it brings up Redis and the workload, which installing Cargo
# needs -- Frappe queues work as part of finishing a site's setup.
as_bench_user "pilot --yes -b $q_bench setup production --admin-domain $q_admin_domain"

# `su -` starts a login shell, so the enrolment variables are exported inside it rather
# than out here.
as_bench_user "$enrolment pilot --yes -b $q_bench install-app $q_site cargo"

# The workers started before Cargo existed, so they carry none of its scheduled jobs.
as_bench_user "pilot --yes -b $q_bench restart"
