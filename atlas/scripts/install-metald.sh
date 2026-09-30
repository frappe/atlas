#!/usr/bin/env bash
# Install metald and its host dependencies. You can run this script again after a failure.

set -eu

: "${METALD_DOWNLOAD_URL:?METALD_DOWNLOAD_URL is required}"
: "${METALD_SHA256:?METALD_SHA256 is required}"
: "${MESH_UPLINK_INTERFACE:?MESH_UPLINK_INTERFACE is required}"
: "${WG_MESH_DOWNLOAD_URL:?WG_MESH_DOWNLOAD_URL is required}"
: "${WG_MESH_SHA256:?WG_MESH_SHA256 is required}"
: "${COORDINATION_LISTEN_ADDRESS:?COORDINATION_LISTEN_ADDRESS is required}"
: "${ATLAS_COMMON_NAME:?ATLAS_COMMON_NAME is required}"

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

if [ "$(id -u)" -ne 0 ]; then
	echo "install-metald must run as root" >&2
	exit 1
fi

step() { echo "==> $*"; }
skip() { echo "    $* is already installed"; }

# reported_version returns a tool version or "unknown".
reported_version() {
	local tool_version
	tool_version=$("$1" version 2>/dev/null | head -1) || tool_version=""
	[ -n "$tool_version" ] || tool_version=unknown
	echo "$tool_version"
}

# service_is_stable checks that the metal service stays active.
service_is_stable() {
	local checks_remaining=5

	while [ "$checks_remaining" -gt 0 ]; do
		systemctl is-active --quiet metal.service || return 1
		checks_remaining=$((checks_remaining - 1))
		[ "$checks_remaining" -eq 0 ] || sleep 1
	done

	return 0
}


step "install required packages"
if ! command -v zpool >/dev/null || ! command -v curl >/dev/null || ! command -v iptables >/dev/null || ! command -v openssl >/dev/null; then
	export DEBIAN_FRONTEND=noninteractive
	apt update -qq
	apt install -y -qq curl iptables openssl tar zfsutils-linux
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


# install_binary replaces the installed binary and keeps the previous copy.
install_binary() {
	local destination=$1
	local source_url=$2
	local expected_hash=$3
	local download

	download=$(mktemp "$destination.staged.XXXXXX")
	trap "rm -f '$download'" EXIT
	curl -fsSL -o "$download" "$source_url"

	# The version command below runs this file, so check it before that.
	download_hash=$(sha256sum "$download" | cut -d' ' -f1)
	if [ "$download_hash" != "$expected_hash" ]; then
		echo "the file at $source_url has hash $download_hash, expected $expected_hash" >&2
		exit 1
	fi
	chmod 0755 "$download"

	if [ -x "$destination" ]; then
		echo "    replacing $(reported_version "$destination") with $(reported_version "$download")"
		cp -a "$destination" "$destination.previous"
	fi

	mv -f "$download" "$destination"
	trap - EXIT
}


step "install metald"
install_binary /usr/bin/metald "$METALD_DOWNLOAD_URL" "$METALD_SHA256"


step "install atlas-wg-mesh"
install -d -m 0755 "$(dirname "$mesh_binary_path")"
install_binary "$mesh_binary_path" "$WG_MESH_DOWNLOAD_URL" "$WG_MESH_SHA256"


step "create directories for metald"
mkdir -p "$machines_dir" "$images_dir"


# mesh_sections appends the WireGuard and Atlas WG Mesh settings. Atlas owns the
# uplink name, because only Atlas knows which interface carries Atlas NDP.
mesh_sections() {
	cat >> "$config_file" <<EOF

[wireguard]
interface = "$wireguard_interface"

[wg_mesh]
binary_path = "$mesh_binary_path"
uplink = "$MESH_UPLINK_INTERFACE"
EOF
}

# tls_sections appends the required TLS file paths.
tls_sections() {
	cat >> "$config_file" <<EOF

[tls]
ca_file = "$base_dir/tls/ca.crt"
certificate_file = "$base_dir/tls/node.crt"
private_key_file = "$base_dir/tls/node.key"
atlas_common_name = "$ATLAS_COMMON_NAME"
EOF
}

step "config ($config_file)"
# Keep the matching config for rollback.
previous_config=$config_file.previous
if [ -f "$config_file" ]; then
	cp -a "$config_file" "$previous_config"
	sed -i "s|^listen[[:space:]]*=.*|listen   = \"$listen_address\"|" "$config_file"
	if grep -q '^coordination_listen[[:space:]]*=' "$config_file"; then
		sed -i "s|^coordination_listen[[:space:]]*=.*|coordination_listen = \"$COORDINATION_LISTEN_ADDRESS\"|" "$config_file"
	else
		sed -i "/^listen[[:space:]]*=/a coordination_listen = \"$COORDINATION_LISTEN_ADDRESS\"" "$config_file"
	fi
	if grep -q '^atlas_common_name[[:space:]]*=' "$config_file"; then
		sed -i "s|^atlas_common_name[[:space:]]*=.*|atlas_common_name = \"$ATLAS_COMMON_NAME\"|" "$config_file"
	elif grep -q '^\[tls\]' "$config_file"; then
		sed -i "/^\[tls\]/a atlas_common_name = \"$ATLAS_COMMON_NAME\"" "$config_file"
	fi
	sed -i "/^auth_token_hash[[:space:]]*=/d" "$config_file"
	if grep -q '^\[wg_mesh\]' "$config_file"; then
		sed -i "s|^uplink = .*|uplink = \"$MESH_UPLINK_INTERFACE\"|" "$config_file"
		sed -i "s|^binary_path = \"/usr/local/bin/atlas-wg-mesh\"|binary_path = \"$mesh_binary_path\"|" "$config_file"
	else
		mesh_sections
	fi
	if ! grep -q '^\[tls\]' "$config_file"; then
		tls_sections
	fi
else
	cat > "$config_file" <<EOF
[metald]
base_dir = "$base_dir"
listen   = "$listen_address"
coordination_listen = "$COORDINATION_LISTEN_ADDRESS"

[firecracker]
binary_path = "/usr/bin/firecracker"
sockets_dir = "$sockets_dir"

[jailer]
binary_path = "/usr/bin/jailer"

[zfs]
pool = "$storage_pool_name"
EOF
	mesh_sections
	tls_sections
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
RequiresMountsFor=$base_dir

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
RequiresMountsFor=$base_dir

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

step "enable IP forwarding"
# A forwarding host ignores router advertisements unless accept_ra is 2, and a
# gateway VM needs its host to hold the IPv6 default route.
public_interface=$(ip -4 route show default | awk 'NR == 1 { print $5 }')
{
	printf 'net.ipv4.ip_forward = 1\n'
	if [ -n "$public_interface" ]; then
		printf 'net.ipv6.conf.%s.accept_ra = 2\n' "$public_interface"
	fi
} > /etc/sysctl.d/99-metald.conf
sysctl -q -p /etc/sysctl.d/99-metald.conf
/usr/local/lib/metal/network-setup


step "enable and start metal service"
systemctl daemon-reload
systemctl enable metal.service
# Restart preserves console descriptors and running VMs.
if ! systemctl restart metal.service || ! service_is_stable; then
	echo "==> metal.service did not stay up; restoring the previous binary and config"
	if [ -f "$previous_config" ]; then
		mv -f "$previous_config" "$config_file"
	fi
	if [ -x /usr/bin/metald.previous ]; then
		mv -f /usr/bin/metald.previous /usr/bin/metald
	fi
	if [ -x "$mesh_binary_path.previous" ]; then
		mv -f "$mesh_binary_path.previous" "$mesh_binary_path"
	fi
	systemctl restart metal.service || true
	journalctl -u metal.service -n 20 --no-pager -o cat >&2 || true
	echo "metal.service did not start with the installed build" >&2
	exit 1
fi

step "running metald $(reported_version /usr/bin/metald)"
