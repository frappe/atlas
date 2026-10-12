#!/usr/bin/env bash

set -eu

netplan_file=/etc/netplan/51-private-network.yaml
networkd_dropin_dir="/etc/systemd/network/10-netplan-$DEVICE.network.d"
networkd_dropin="$networkd_dropin_dir/20-atlas-proxy-ndp.conf"

if ! command -v netplan >/dev/null; then
    echo "netplan is required to configure the private network" >&2
    exit 1
fi

mkdir -p /etc/netplan

umask 077

cat > "$netplan_file" <<EOF
network:
  version: 2
  ethernets:
    $PARENT_INTERFACE:
      dhcp4: true
      dhcp6: true
  vlans:
    $DEVICE:
      id: $VLAN
      link: $PARENT_INTERFACE
      mtu: $MTU
      addresses:
        - $ADDRESS
EOF

chmod 600 "$netplan_file"

netplan generate

mkdir -p "$networkd_dropin_dir"
chmod 755 "$networkd_dropin_dir"

cat > "$networkd_dropin" <<EOF
[Network]
IPv6ProxyNDP=yes
EOF

chmod 644 "$networkd_dropin"

if ! ip link show "$DEVICE" >/dev/null 2>&1; then
    ip link add link "$PARENT_INTERFACE" name "$DEVICE" type vlan id "$VLAN"
fi

ip link set dev "$DEVICE" mtu "$MTU" up
ip addr replace "$ADDRESS" dev "$DEVICE"

ip -4 -o addr show dev "$DEVICE" scope global |
    awk 'NR == 1 {print $4}' |
    cut -d/ -f1
