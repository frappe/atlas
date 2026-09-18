#!/usr/bin/env bash
# Install metald and its host dependencies. You can run this script again after a failure.

set -eu

: "${METALD_DOWNLOAD_URL:?METALD_DOWNLOAD_URL is required}"
: "${METALD_AUTH_TOKEN_HASH:?METALD_AUTH_TOKEN_HASH is required}"
: "${STORAGE_POOL_DEVICE:?STORAGE_POOL_DEVICE is required}"
: "${MESH_UPLINK_INTERFACE:?MESH_UPLINK_INTERFACE is required}"
: "${WG_MESH_DOWNLOAD_URL:?WG_MESH_DOWNLOAD_URL is required}"

storage_pool_name=${STORAGE_POOL_NAME:-metal}
firecracker_version=${FIRECRACKER_VERSION:-v1.16.1}
listen_address=${LISTEN_ADDRESS:?LISTEN_ADDRESS is required}
wireguard_interface=${WIREGUARD_INTERFACE:-wg0}
mesh_binary_path=${MESH_BINARY_PATH:-/usr/local/bin/atlas-wg-mesh}

base_dir=/var/lib/metal
machines_dir=$base_dir/machines
images_dir=$base_dir/images
sockets_dir=/run/metal
config_file=$base_dir/metald.toml
unicast_peers_file=$base_dir/unicast-peers
unicast_unit=/etc/systemd/system/atlas-wg-mesh-unicast.service

if [ "$(id -u)" -ne 0 ]; then
	echo "install-metald must run as root" >&2
	exit 1
fi

step() { echo "==> $*"; }
skip() { echo "    $* is already installed"; }

# kept_binaries lists existing tools that this script leaves in place.
kept_binaries=""

# reported_version returns a tool version or "unknown".
reported_version() {
	local tool_version
	tool_version=$("$1" version 2>/dev/null | head -1) || tool_version=""
	[ -n "$tool_version" ] || tool_version=unknown
	echo "$tool_version"
}


kernel_release=$(uname -r)

step "install required packages"
if ! command -v zpool >/dev/null || ! command -v curl >/dev/null || ! command -v iptables >/dev/null ||
	! command -v make >/dev/null || ! command -v cc >/dev/null || ! command -v pahole >/dev/null ||
	[ ! -d "/lib/modules/$kernel_release/build" ]; then
	export DEBIAN_FRONTEND=noninteractive
	apt update -qq
	apt install -y -qq curl iptables tar zfsutils-linux build-essential dwarves "linux-headers-$kernel_release"
else
	skip "packages"
fi


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


# install_binary downloads one tool. An existing tool is left alone, because
# replacing a running daemon is an upgrade and needs its own steps.
install_binary() {
	local destination=$1
	local source_url=$2
	local binary_name binary_version download

	if [ -x "$destination" ]; then
		binary_name=$(basename "$destination")
		binary_version=$(reported_version "$destination")
		echo "    $binary_name is already installed; it reports: $binary_version"
		kept_binaries="$kept_binaries$binary_name reports: $binary_version
"
		return
	fi
	download=$(mktemp)
	trap 'rm -f "$download"' EXIT
	curl -fsSL -o "$download" "$source_url"
	install -m 755 "$download" "$destination"
	rm -f "$download"
	trap - EXIT
}


step "install metald"
install_binary /usr/bin/metald "$METALD_DOWNLOAD_URL"


step "install atlas-wg-mesh"
install -d -m 0755 "$(dirname "$mesh_binary_path")"
install_binary "$mesh_binary_path" "$WG_MESH_DOWNLOAD_URL"


# The Atlas neighbour kernel module must be loaded before metald starts,
# because Atlas WG Mesh refuses to configure a host without its kfunc. The
# module builds against the running kernel headers on this host. An old mesh
# binary has no module command. Skip the step for that binary, because this
# script does not upgrade an installed binary.
step "install Atlas neighbour kernel module"
if "$mesh_binary_path" module --help >/dev/null 2>&1; then
	"$mesh_binary_path" module install
else
	echo "    atlas-wg-mesh has no module command. Skip the neighbour kernel module." >&2
fi


step "create directories for metald"
mkdir -p "$machines_dir" "$images_dir"


step "zfs pool ($storage_pool_name)"
if zpool list "$storage_pool_name" >/dev/null 2>&1; then
	skip "pool $storage_pool_name"
else
	zpool create -m none "$storage_pool_name" "$STORAGE_POOL_DEVICE"
fi
zfs list "$storage_pool_name/images" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/images"
zfs list "$storage_pool_name/vms" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/vms"
zfs list "$storage_pool_name/staging" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/staging"
zfs list "$storage_pool_name/warm" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/warm"


# mesh_sections appends the WireGuard and Atlas WG Mesh settings. Atlas owns the
# uplink name, because only Atlas knows which interface carries Atlas NDP.
mesh_sections() {
	cat >> "$config_file" <<EOF

[wireguard]
interface = "$wireguard_interface"

[wg_mesh]
binary_path = "$mesh_binary_path"
uplink = "$MESH_UPLINK_INTERFACE"
peers_file = "$unicast_peers_file"
EOF
}

# ensure_mesh_peers_file_setting keeps the unicast peer file path in an existing
# configuration, because metald and the unicast unit must agree on it.
ensure_mesh_peers_file_setting() {
	if grep -q '^peers_file = ' "$config_file"; then
		sed -i "s|^peers_file = .*|peers_file = \"$unicast_peers_file\"|" "$config_file"
	else
		sed -i "/^\\[wg_mesh\\]/a peers_file = \"$unicast_peers_file\"" "$config_file"
	fi
}

step "config ($config_file)"
if [ -f "$config_file" ]; then
	sed -i "s|^listen[[:space:]]*=.*|listen   = \"$listen_address\"|" "$config_file"
	if grep -q '^auth_token_hash =' "$config_file"; then
		sed -i "s/^auth_token_hash = .*/auth_token_hash = \"$METALD_AUTH_TOKEN_HASH\"/" "$config_file"
	else
		sed -i "/^listen[[:space:]]*=/a auth_token_hash = \"$METALD_AUTH_TOKEN_HASH\"" "$config_file"
	fi
	if grep -q '^\[wg_mesh\]' "$config_file"; then
		sed -i "s|^uplink = .*|uplink = \"$MESH_UPLINK_INTERFACE\"|" "$config_file"
		sed -i "s|^binary_path = \"/usr/local/bin/atlas-wg-mesh\"|binary_path = \"$mesh_binary_path\"|" "$config_file"
		ensure_mesh_peers_file_setting
	else
		mesh_sections
	fi
else
	cat > "$config_file" <<EOF
[metald]
base_dir = "$base_dir"
listen   = "$listen_address"
auth_token_hash = "$METALD_AUTH_TOKEN_HASH"

[firecracker]
binary_path = "/usr/bin/firecracker"
sockets_dir = "$sockets_dir"

[jailer]
binary_path = "/usr/bin/jailer"

[zfs]
pool = "$storage_pool_name"
EOF
	mesh_sections
	chmod 600 "$config_file"
fi


step "network setup"
install -d -m 0755 /usr/local/lib/metal
cat > /usr/local/lib/metal/network-setup <<'EOF'
#!/bin/sh
set -eu

uplink=$(ip -4 route show default | awk 'NR == 1 { print $5 }')
[ -n "$uplink" ] || {
	echo "metald network setup requires an IPv4 default route" >&2
	exit 1
}

iptables -t nat -C POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE 2>/dev/null ||
	iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE
EOF
chmod 0755 /usr/local/lib/metal/network-setup


step "systemd units"
cat > /etc/systemd/system/metal.service <<EOF
[Unit]
Description=metal daemon
Wants=network-online.target
After=network-online.target

[Service]
ExecStartPre=/usr/local/lib/metal/network-setup
ExecStart=/usr/bin/metald serve --config $config_file
Restart=on-failure
RestartSec=1
# Keep PTY masters across a metald restart or stop.
NotifyAccess=main
FileDescriptorStoreMax=1024
FileDescriptorStorePreserve=yes

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/metal-vm@.service <<EOF
[Unit]
Description=metal microVM %i
After=network.target

[Service]
Type=exec
EnvironmentFile=$machines_dir/%i/jailer.env
ExecStart=/usr/bin/jailer \$JAILER_ARGS
StandardInput=tty-force
StandardOutput=tty
StandardError=journal
TTYPath=/run/metal/consoles/%i
TTYReset=yes
TTYVHangup=yes
Restart=no
EOF

# The unicast daemon replaces the multicast NDP filters with the unicast hooks.
# It stays disabled here: metald enables it when the controller syncs a unicast
# peer set, and disables it when the controller returns to multicast.
cat > "$unicast_unit" <<EOF
[Unit]
Description=Atlas WG Mesh unicast NDP transport
Requires=metal.service
After=metal.service network-online.target
Wants=network-online.target

[Service]
# metald configures Atlas WG Mesh on every boot, and the daemon must wait for
# that, because its start removes the multicast NDP filters that configure
# attaches.
ExecStartPre=/bin/sh -c 'for waiting_second in \$(seq 30); do "$mesh_binary_path" status >/dev/null 2>&1 && exit 0; sleep 1; done; echo "Atlas WG Mesh is not configured" >&2; exit 1'
ExecStart=$mesh_binary_path unicast start $unicast_peers_file
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF


step "enable IP forwarding"
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/99-metald.conf
sysctl -q -w net.ipv4.ip_forward=1
/usr/local/lib/metal/network-setup


step "enable and start metal service"
systemctl daemon-reload
systemctl enable metal.service
systemctl restart metal.service
systemctl is-active metal.service


if [ -n "$kept_binaries" ]; then
	step "binaries that this script did not upgrade"
	printf '%s' "$kept_binaries" | while IFS= read -r kept_binary; do
		echo "    $kept_binary"
	done
	echo "    This script installs a missing binary only. It does not upgrade an installed binary."
	echo "    A tool that reports unknown has no version command. It can be an old build."
fi
