#!/usr/bin/env bash
# Install Pilot in the Atlas VM, create the bench and the site, and keep it up.
set -euo pipefail

bench_name=atlas-bench
site_name=atlas.localhost
source_path=/opt/atlas-source
branch=develop
admin_password=admin
bench_user=frappe
pilot_install_url="${PILOT_INSTALL_URL:-https://raw.githubusercontent.com/frappe/pilot/develop/install.sh}"

step() { echo "==> $*"; }
fail() { echo "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--bench) bench_name=$2; shift 2 ;;
		--site) site_name=$2; shift 2 ;;
		--source) source_path=$2; shift 2 ;;
		--branch) branch=$2; shift 2 ;;
		--admin-password) admin_password=$2; shift 2 ;;
		*) fail "unknown argument: $1" ;;
	esac
done

[[ $EUID -eq 0 ]] || fail "run this inside the VM as root"
[[ -d $source_path ]] || fail "no such source directory: $source_path"

pilot_home="/home/$bench_user/pilot"
bench_path="$pilot_home/benches/$bench_name"
application_path="$bench_path/apps/atlas"
site_path="$bench_path/sites/$site_name"

as_bench_user() {
	su - "$bench_user" -c "$1"
}

wait_for_name_resolution() {
	for _ in $(seq 1 30); do
		getent hosts archive.ubuntu.com >/dev/null 2>&1 && return 0
		sleep 2
	done
	fail "the VM has no working name resolution"
}

# `after_install` for the atlas app builds metald and Atlas WG Mesh, so the
# guest needs the same build tools a Metal host build needs.
install_packages() {
	step "install the base packages"
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq curl git ca-certificates make clang libbpf-dev linux-libc-dev
}

# This is a development VM. Passwordless sudo keeps every Pilot step that needs
# a package or a service from stopping on a password prompt.
create_bench_user() {
	id "$bench_user" >/dev/null 2>&1 && return 0
	step "create the $bench_user user"
	useradd -m -s /bin/bash "$bench_user"
	echo "$bench_user ALL=(ALL) NOPASSWD: ALL" > "/etc/sudoers.d/$bench_user-atlas-vm"
	chmod 440 "/etc/sudoers.d/$bench_user-atlas-vm"
}

install_pilot() {
	if [[ ! -x $pilot_home/bin/pilot ]]; then
		step "install the Pilot host stack"
		curl -fsSL "$pilot_install_url" | bash
		step "install Pilot for $bench_user"
		as_bench_user "curl -fsSL '$pilot_install_url' | bash"
	fi
	[[ -x $pilot_home/bin/pilot ]] || fail "the Pilot installer did not produce $pilot_home/bin/pilot"
}

create_bench() {
	[[ -f $bench_path/bench.toml ]] && return 0
	step "create the $bench_name bench"
	as_bench_user "pilot new '$bench_name' --admin-password '$admin_password'"
}

# Pilot clones the app, registers it, and installs it into the bench
# environment. The clone holds the last commit, so the working tree the host
# sent is copied over it afterwards.
install_application() {
	if [[ ! -d $application_path ]]; then
		step "install the atlas app from $source_path"
		as_bench_user "pilot -b '$bench_name' get-app '$source_path' --branch '$branch'"
	fi

	step "apply the working tree"
	tar -C "$source_path" --exclude=.git -cf - . | tar -C "$application_path" --no-same-owner -xf -
	chown -R "$bench_user:$bench_user" "$application_path"
}

create_site() {
	[[ -d $site_path ]] && return 0
	step "create $site_name"
	as_bench_user "pilot -b '$bench_name' new-site '$site_name' \
		--apps atlas --admin-password '$admin_password'"
}

# The VM is reached by address and not by site name, so the site has to be the
# one Frappe serves for any host header.
set_default_site() {
	step "set the default site"
	python3 - "$bench_path/sites/common_site_config.json" "$site_name" <<'PYTHON'
import json
import sys

path, site = sys.argv[1], sys.argv[2]
with open(path) as configuration_file:
	configuration = json.load(configuration_file)

configuration["default_site"] = site
with open(path, "w") as configuration_file:
	json.dump(configuration, configuration_file, indent=1, sort_keys=True)
PYTHON
	chown "$bench_user:$bench_user" "$bench_path/sites/common_site_config.json"
}

install_service() {
	step "install the bench service"
	cat > /etc/systemd/system/atlas-bench.service <<UNIT
[Unit]
Description=Atlas bench
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
Type=simple
User=$bench_user
WorkingDirectory=$pilot_home
Environment=HOME=/home/$bench_user
Environment=PATH=/home/$bench_user/.local/bin:$pilot_home/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$pilot_home/bin/pilot -b $bench_name start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

	systemctl daemon-reload
	systemctl enable atlas-bench.service
	systemctl restart atlas-bench.service
}

wait_for_name_resolution
install_packages
create_bench_user
install_pilot
create_bench
install_application
create_site
set_default_site
install_service

step "the site is up"
