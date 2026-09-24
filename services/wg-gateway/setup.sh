#!/usr/bin/env bash
# Install the Atlas WireGuard gateway on Ubuntu 24.04. You can run this script again.

set -euo pipefail

: "${REGION_ID:?REGION_ID is required}"
: "${GATEWAY_MESH:?GATEWAY_MESH is required}"
: "${LISTEN_PORT:?LISTEN_PORT is required}"

source_dir=$(cd "$(dirname "$0")" && pwd)
state_dir=/opt/atlas/wg-gateway

if [ "$(id -u)" -ne 0 ]; then
	echo "setup.sh must run as root" >&2
	exit 1
fi

step() { echo "==> $*"; }


step "install packages"
export DEBIAN_FRONTEND=noninteractive
# needrestart would restart ssh and systemd-networkd during the install.
export NEEDRESTART_SUSPEND=1
apt-get update -qq
apt-get install -y -qq wireguard-tools nftables iproute2


step "enable IPv6 forwarding"
cat > /etc/sysctl.d/90-atlas-wg-gateway.conf <<'SYSCTL'
net.ipv6.conf.all.forwarding = 1
SYSCTL
sysctl -q -p /etc/sysctl.d/90-atlas-wg-gateway.conf


step "create the WireGuard interface"
install -d -m 0750 "$state_dir"
if [ ! -f "$state_dir/privatekey" ]; then
	wg genkey | tee "$state_dir/privatekey" | wg pubkey > "$state_dir/publickey"
	chmod 0600 "$state_dir/privatekey"
fi
# Atlas overwrites peers.conf on every peer change; an empty file means no peers.
if [ ! -f "$state_dir/peers.conf" ]; then
	printf '# Managed by Atlas. Do not edit.\n' > "$state_dir/peers.conf"
fi
ip link add wg0 type wireguard 2>/dev/null || true
wg set wg0 private-key "$state_dir/privatekey" listen-port "$LISTEN_PORT"
ip link set wg0 up
wg setconf wg0 "$state_dir/peers.conf"
ip -6 route replace fdac::/16 dev wg0
ip -6 route replace default via fe80::1 dev eth0


step "write the firewall"
# Atlas replaces this file on every peer change. The boot copy drops new
# client traffic and SNATs nothing, so a fresh gateway is closed by default.
cat > "$state_dir/gateway.nft" <<EOF
table ip6 atlas_wg_gateway {}
delete table ip6 atlas_wg_gateway

table ip6 atlas_wg_gateway {
	chain forward {
		type filter hook forward priority filter; policy drop;
		ct state established,related counter accept
	}

	chain postrouting {
		type nat hook postrouting priority srcnat; policy accept;
		ip6 saddr fdac::/16 ip6 daddr fdaa::/16 counter snat to $GATEWAY_MESH
	}

	# The gateway forwards out of the interface that received the packet. A redirect would send the host around it.
	chain output {
		type filter hook output priority filter; policy accept;
		icmpv6 type nd-redirect drop
	}
}
EOF
nft -f "$state_dir/gateway.nft"


step "start the gateway"
sed -e "s|@LISTEN_PORT@|$LISTEN_PORT|" \
	"$source_dir/systemd/atlas-wg-gateway.service" > /etc/systemd/system/atlas-wg-gateway.service
systemctl daemon-reload
systemctl enable atlas-wg-gateway.service
systemctl restart atlas-wg-gateway.service


step "check the gateway"
wg show wg0 >/dev/null
nft list table ip6 atlas_wg_gateway >/dev/null

echo "the WireGuard gateway listens on port $LISTEN_PORT and SNATs fdac::/16 to $GATEWAY_MESH"
