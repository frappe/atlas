#!/usr/bin/env bash
# Install the host input firewall. It covers traffic to the host itself. Metal filters forwarded guest traffic.

set -eu

: "${ATLAS_WIREGUARD_ADDRESS:?ATLAS_WIREGUARD_ADDRESS is required}"
: "${MESH_UPLINK_INTERFACE:?MESH_UPLINK_INTERFACE is required}"
: "${PRIVATE_NETWORK_CIDR:?PRIVATE_NETWORK_CIDR is required}"

wireguard_interface=${WIREGUARD_INTERFACE:-wg0}
wireguard_port=${WIREGUARD_LISTEN_PORT:-51820}
rules_file=/etc/atlas/host-firewall.nft

if [ "$(id -u)" -ne 0 ]; then
	echo "install-host-firewall must run as root" >&2
	exit 1
fi

wireguard_rule="iifname \"$MESH_UPLINK_INTERFACE\" ip saddr $PRIVATE_NETWORK_CIDR udp dport $wireguard_port accept"

if ! command -v nft >/dev/null; then
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq nftables
fi

install -d -m 0755 /etc/atlas
cat > "$rules_file.staged" <<EOF
table inet atlas_host
delete table inet atlas_host

table inet atlas_host {
	chain input {
		type filter hook input priority filter; policy drop;
		iifname "lo" accept
		ct state established,related accept
		ct state invalid drop
		meta l4proto { icmp, ipv6-icmp } accept
		udp sport 67 udp dport 68 accept
		udp sport 547 udp dport 546 accept
		$wireguard_rule
		# The atlas-vm guest reaches this host's WireGuard directly.
		iifname "tap-atlas" udp dport $wireguard_port accept
		iifname "$wireguard_interface" ip6 saddr $ATLAS_WIREGUARD_ADDRESS tcp dport { 22, 9000 } accept
		iifname "$wireguard_interface" ip6 saddr fdab::/16 tcp dport { 9001, 9002 } accept
		# Recovery path when wg0 is down.
		iifname "$MESH_UPLINK_INTERFACE" ip saddr $PRIVATE_NETWORK_CIDR tcp dport 22 accept
	}
}
EOF
nft -c -f "$rules_file.staged"
mv -f "$rules_file.staged" "$rules_file"

cat > /etc/systemd/system/atlas-host-firewall.service <<EOF
[Unit]
Description=Atlas host input firewall
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $rules_file
ExecReload=/usr/sbin/nft -f $rules_file

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable atlas-host-firewall.service
systemctl restart atlas-host-firewall.service
echo "==> host firewall active"
