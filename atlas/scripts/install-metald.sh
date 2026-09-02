#!/usr/bin/env bash
#
# Installs metald and its host dependencies.
# The script is idempotent and can be run multiple times in case of failure.

set -eu

: "${METALD_DOWNLOAD_URL:?METALD_DOWNLOAD_URL is required}"
: "${STORAGE_POOL_DEVICE:?STORAGE_POOL_DEVICE is required}"

storage_pool_name=${STORAGE_POOL_NAME:-metal}
firecracker_version=${FIRECRACKER_VERSION:-latest}
listen_address=${LISTEN_ADDRESS:-0.0.0.0:9001}

base_dir=/var/lib/metal
machines_dir=$base_dir/machines
kernel_dir=$base_dir/kernels
sockets_dir=/run/metal
config_file=$base_dir/metald.toml

if [ "$(id -u)" -ne 0 ]; then
	echo "install-metald must run as root" >&2
	exit 1
fi

step() { echo "==> $*"; }
skip() { echo "    $* is already installed"; }

# Install required packages
step "install required packages"
if ! command -v zpool >/dev/null || ! command -v curl >/dev/null; then
	export DEBIAN_FRONTEND=noninteractive
	apt update -qq
	apt install -y -qq curl tar zfsutils-linux
else
	skip "packages"
fi

# Install firecracker and jailer
step "install firecracker and jailer"
if [ -x /usr/bin/firecracker ] && [ -x /usr/bin/jailer ]; then
	skip "firecracker $(/usr/bin/firecracker --version | head -1)"
else
	architecture=$(uname -m)
	releases=https://api.github.com/repos/firecracker-microvm/firecracker/releases
	if [ "$firecracker_version" = "latest" ]; then
		firecracker_version=$(curl -fsSL "$releases/latest" |
			sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)
		[ -n "$firecracker_version" ] || {
			echo "could not resolve the latest firecracker release" >&2
			exit 1
		}
	fi
	echo "    installing firecracker $firecracker_version"

	download_directory=$(mktemp -d)
	trap 'rm -rf "$download_directory"' EXIT
	archive=firecracker-$firecracker_version-$architecture.tgz
	curl -fsSL -o "$download_directory/$archive" \
		"https://github.com/firecracker-microvm/firecracker/releases/download/$firecracker_version/$archive"
	tar -xzf "$download_directory/$archive" -C "$download_directory"

	binary_directory=$download_directory/release-$firecracker_version-$architecture
	install -m 755 "$binary_directory/firecracker-$firecracker_version-$architecture" /usr/bin/firecracker
	install -m 755 "$binary_directory/jailer-$firecracker_version-$architecture" /usr/bin/jailer
	rm -rf "$download_directory"
	trap - EXIT
fi

# Install metald binary
step "install metald"
if [ -x /usr/bin/metald ]; then
	skip "metald"
else
	metald_download=$(mktemp)
	trap 'rm -f "$metald_download"' EXIT
	curl -fsSL -o "$metald_download" "$METALD_DOWNLOAD_URL"
	install -m 755 "$metald_download" /usr/bin/metald
	rm -f "$metald_download"
	trap - EXIT
fi

# Create directories for metald
step "create directories for metald"
mkdir -p "$machines_dir" "$kernel_dir"

# Init ZFS pool if needed
step "zfs pool ($storage_pool_name)"
if zpool list "$storage_pool_name" >/dev/null 2>&1; then
	skip "pool $storage_pool_name"
else
	zpool create -m none "$storage_pool_name" "$STORAGE_POOL_DEVICE"
fi
zfs list "$storage_pool_name/images" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/images"
zfs list "$storage_pool_name/vms" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/vms"

# Create metald config file
step "config ($config_file)"
if [ -f "$config_file" ]; then
	skip "$config_file"
else
	cat > "$config_file" <<EOF
[metald]
base_dir = "$base_dir"
listen   = "$listen_address"

[firecracker]
binary_path = "/usr/bin/firecracker"
sockets_dir = "$sockets_dir"

[jailer]
binary_path = "/usr/bin/jailer"

[zfs]
pool = "$storage_pool_name"
EOF
	chmod 600 "$config_file"
fi

# Create systemd units for metald and its microVMs
step "systemd units"
if [ -f /etc/systemd/system/metal.service ]; then
	skip "metal.service"
else
	cat > /etc/systemd/system/metal.service <<EOF
[Unit]
Description=metal daemon
After=network.target

[Service]
ExecStart=/usr/bin/metald serve --config $config_file
Restart=on-failure
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF
fi

if [ -f /etc/systemd/system/metal-vm@.service ]; then
	skip "metal-vm@.service"
else
	cat > /etc/systemd/system/metal-vm@.service <<EOF
[Unit]
Description=metal microVM %i
After=network.target

[Service]
Type=exec
EnvironmentFile=$machines_dir/%i/jailer.env
ExecStart=/usr/bin/jailer \$JAILER_ARGS
Restart=no
EOF
fi

# Enable IP forwarding
step "enable IP forwarding"
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/99-metald.conf
sysctl -q -w net.ipv4.ip_forward=1

# Enable and start metald service
step "enable and start metal service"
systemctl daemon-reload
systemctl enable --now metal.service
systemctl is-active metal.service
