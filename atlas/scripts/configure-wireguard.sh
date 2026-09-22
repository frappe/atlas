#!/usr/bin/env bash

set -eu

: "${WIREGUARD_ADDRESS:?WIREGUARD_ADDRESS is required}"
: "${MESH_UPLINK_INTERFACE:?MESH_UPLINK_INTERFACE is required}"

interface=${WIREGUARD_INTERFACE:-wg0}
listen_port=${WIREGUARD_LISTEN_PORT:-51820}
config_file=/etc/wireguard/$interface.conf
private_key_file=/etc/wireguard/$interface.key

if [ "$(id -u)" -ne 0 ]; then
	echo "configure-wireguard must run as root" >&2
	exit 1
fi

# A host address belongs to fdab::/16. VMs use fdaa::/16, and the mesh drops
# VM traffic to the host range.
case "$WIREGUARD_ADDRESS" in
fdab:*) ;;
*)
	echo "WIREGUARD_ADDRESS must be inside fdab::/16, got $WIREGUARD_ADDRESS" >&2
	exit 1
	;;
esac

step() { echo "==> $*" >&2; }

# The tunnel crosses the mesh uplink. The overhead is one IPv4 header, one UDP
# header, and the WireGuard header.
mtu_file=/sys/class/net/$MESH_UPLINK_INTERFACE/mtu
if [ ! -r "$mtu_file" ]; then
	echo "mesh uplink $MESH_UPLINK_INTERFACE has no MTU" >&2
	exit 1
fi
wireguard_mtu=$(($(cat "$mtu_file") - 20 - 8 - 32))

if [ "$wireguard_mtu" -lt 1280 ]; then
	echo "$MESH_UPLINK_INTERFACE leaves $wireguard_mtu for WireGuard, below the 1280 IPv6 minimum" >&2
	exit 1
fi

step "packages"
if ! command -v wg >/dev/null; then
	export DEBIAN_FRONTEND=noninteractive
		apt update -qq
		apt install -y -qq wireguard-tools
fi

step "private key ($private_key_file)"
install -d -m 700 /etc/wireguard
if [ ! -f "$private_key_file" ]; then
	(umask 077 && wg genkey > "$private_key_file")
fi

step "config ($config_file)"
# The region prefix makes every peer on-link. The daemon adds peers, and
# `wg set` installs no route of its own.
config="[Interface]
Address = $WIREGUARD_ADDRESS/32
ListenPort = $listen_port
MTU = $wireguard_mtu
PostUp = wg set %i private-key $private_key_file"

# A reused host keeps the config of its earlier registration, so rewrite a stale one.
is_config_changed=false
if [ ! -f "$config_file" ] || [ "$(cat "$config_file")" != "$config" ]; then
	(umask 077 && printf '%s\n' "$config" > "$config_file")
	is_config_changed=true
fi

step "interface ($interface)"
systemctl enable --now "wg-quick@$interface"
if [ "$is_config_changed" = true ]; then
	systemctl restart "wg-quick@$interface"
fi
systemctl is-active "wg-quick@$interface" >/dev/null


# !!! DON'T CHANGE THE FORMAT OF BELOW OUTPUT !!!

echo "===PUBLIC_KEY_START==="
wg pubkey < "$private_key_file"
echo "===PUBLIC_KEY_END==="
