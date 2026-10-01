#!/usr/bin/env bash
# Apply one Atlas wg-quick file now and within 10 seconds of each Atlas rewrite. Run as root on the Atlas machine.

set -eu

config_file=${1:?usage: install-atlas-wireguard.sh /path/to/sites/SITE/private/wireguard/atlas0.conf}
interface=$(basename "$config_file" .conf)
unit=atlas-wireguard-$interface
# Tenant-0 VMs reach Atlas through this interface. WireGuard itself needs no port here.
tcp_ports=${ATLAS_WIREGUARD_TCP_PORTS:-22, 80, 443, 2222}
rules_file=/etc/atlas/$unit.nft

if [ "$(id -u)" -ne 0 ]; then
	echo "install-atlas-wireguard must run as root" >&2
	exit 1
fi
if [ ! -f "$config_file" ]; then
	echo "$config_file does not exist. Run pilot --site SITE configure-atlas-wireguard first." >&2
	exit 1
fi
if ! command -v nft >/dev/null; then
	echo "install-atlas-wireguard needs nft. Install nftables first." >&2
	exit 1
fi

install -d -m 0755 /etc/atlas
cat > "$rules_file" <<EOF
table inet atlas_$interface
delete table inet atlas_$interface

table inet atlas_$interface {
	chain input {
		type filter hook input priority filter; policy accept;
		iifname "$interface" ct state established,related accept
		iifname "$interface" meta l4proto ipv6-icmp accept
		iifname "$interface" tcp dport { $tcp_ports } accept
		iifname "$interface" drop
	}
}
EOF
nft -c -f "$rules_file"

cat > /etc/systemd/system/$unit.service <<EOF
[Unit]
Description=Apply the Atlas WireGuard file $interface.conf
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
LogLevelMax=notice
ExecStartPre=$(command -v nft) -f $rules_file
# SELinux lets wg-quick read only /etc/wireguard.
ExecStart=/usr/bin/bash -c 'cmp -s $config_file /etc/wireguard/$interface.conf && ip link show $interface >/dev/null 2>&1 && exit 0; install -m 0600 $config_file /etc/wireguard/$interface.conf; if ip link show $interface >/dev/null 2>&1; then wg syncconf $interface <(wg-quick strip $interface); else wg-quick up $interface; fi'
EOF

# SELinux can hide a home directory from a path unit, so a timer polls the file.
cat > /etc/systemd/system/$unit.timer <<EOF
[Unit]
Description=Apply the Atlas WireGuard file $interface.conf every 10 seconds

[Timer]
OnActiveSec=0
OnUnitInactiveSec=10s
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "$unit.timer"
systemctl start "$unit.service"
wg show "$interface"
