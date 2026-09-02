#!/usr/bin/env bash

set -eu

: "${WIREGUARD_ADDRESS:?WIREGUARD_ADDRESS is required}"
: "${WIREGUARD_MTU:?WIREGUARD_MTU is required}"

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

if [ "$WIREGUARD_MTU" -lt 1280 ]; then
	echo "WIREGUARD_MTU must be at least 1280 for IPv6, got $WIREGUARD_MTU" >&2
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
if [ ! -f "$config_file" ]; then
		# The mesh gives each host a /128. The daemon adds peers.
	cat > "$config_file" <<EOF
[Interface]
Address = $WIREGUARD_ADDRESS/128
ListenPort = $listen_port
MTU = $WIREGUARD_MTU
PostUp = wg set %i private-key $private_key_file
EOF
	chmod 600 "$config_file"
fi

step "interface ($interface)"
systemctl enable --now "wg-quick@$interface"
systemctl is-active "wg-quick@$interface" >/dev/null


# !!! DON'T CHANGE THE FORMAT OF BELOW OUTPUT !!!

echo "===PUBLIC_KEY_START==="
wg pubkey < "$private_key_file"
echo "===PUBLIC_KEY_END==="
