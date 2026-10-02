#!/usr/bin/env bash

set -eu

: "${WIREGUARD_ADDRESS:?WIREGUARD_ADDRESS is required}"
: "${MESH_UPLINK_INTERFACE:?MESH_UPLINK_INTERFACE is required}"
: "${ATLAS_WIREGUARD_ADDRESS:?ATLAS_WIREGUARD_ADDRESS is required}"
: "${ATLAS_WIREGUARD_PUBLIC_KEY:?ATLAS_WIREGUARD_PUBLIC_KEY is required}"

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

case "$ATLAS_WIREGUARD_ADDRESS" in
fdaa:*) ;;
*)
	echo "ATLAS_WIREGUARD_ADDRESS must be inside fdaa::/16, got $ATLAS_WIREGUARD_ADDRESS" >&2
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
# The region prefix makes every peer on-link. The daemon adds host peers, and
# `wg set` installs no route of its own.
interface_config="[Interface]
Address = $WIREGUARD_ADDRESS/32
ListenPort = $listen_port
MTU = $wireguard_mtu
PostUp = wg set %i private-key $private_key_file"

config="$interface_config

[Peer]
PublicKey = $ATLAS_WIREGUARD_PUBLIC_KEY
AllowedIPs = $ATLAS_WIREGUARD_ADDRESS/128"

# A reused host keeps the config of its earlier registration, so rewrite a stale one.
# A wg0 restart drops the host peers until the next sync.
previous_config=$(cat "$config_file" 2>/dev/null || true)
previous_atlas_public_key=$(printf '%s\n' "$previous_config" | sed -n 's/^PublicKey = //p')
is_interface_changed=false
if [ "$(printf '%s\n' "$previous_config" | sed '/^$/,$d')" != "$interface_config" ]; then
	is_interface_changed=true
fi
if [ "$previous_config" != "$config" ]; then
	(umask 077 && printf '%s\n' "$config" > "$config_file")
fi

step "interface ($interface)"
systemctl enable --now "wg-quick@$interface"
if [ "$is_interface_changed" = true ]; then
	systemctl restart "wg-quick@$interface"
fi
systemctl is-active "wg-quick@$interface" >/dev/null

step "Atlas peer ($ATLAS_WIREGUARD_ADDRESS)"
if [ -n "$previous_atlas_public_key" ] && [ "$previous_atlas_public_key" != "$ATLAS_WIREGUARD_PUBLIC_KEY" ]; then
	wg set "$interface" peer "$previous_atlas_public_key" remove
fi
wg set "$interface" peer "$ATLAS_WIREGUARD_PUBLIC_KEY" allowed-ips "$ATLAS_WIREGUARD_ADDRESS/128"
ip -6 route replace "$ATLAS_WIREGUARD_ADDRESS/128" dev "$interface"


# !!! DON'T CHANGE THE FORMAT OF BELOW OUTPUT !!!

echo "===PUBLIC_KEY_START==="
wg pubkey < "$private_key_file"
echo "===PUBLIC_KEY_END==="
