#!/bin/bash
# Host-side network for a VM. Invoked by ExecStartPre in the systemd unit
# (must run before firecracker's ExecStart so the tap exists with vnet_hdr).
# Reads /var/lib/atlas/virtual-machines/$1/network.env. Idempotent.
#
# Approach: the server has DigitalOcean's /64 prefix routed to it, but only a
# /124 is *usable* for routing onward (DO routes the /64 to the droplet, and
# the rest of the /64 has no route inside DO's fabric). So we hand out
# addresses inside a fixed /124 carved from the /64, and we use proxy-NDP on
# the uplink to make the upstream router believe each VM address is on-link.
# The host side of every tap gets fe80::1 link-local so the guest can use
# fe80::1 as its default gateway without us needing to assign a routable
# address to the tap.

set -euo pipefail

virtual_machine_name="${1:?virtual machine name required}"
. "/var/lib/atlas/virtual-machines/${virtual_machine_name}/network.env"

: "${TAP_DEVICE:?missing in network.env}"
: "${VIRTUAL_MACHINE_IPV6:?missing in network.env}"
: "${IPV4_HOST_CIDR:?missing in network.env}"

uplink="$(ip -j -6 route show default | jq -r '.[0].dev')"
# The default-route dev for v4 egress (may differ from the v6 uplink on a
# multi-homed host); used for the masquerade rule.
ipv4_uplink="$(ip -j route show default | jq -r '.[0].dev')"

# Idempotent nftables scaffold. The bootstrap script creates these on first
# install, but they're not persisted across host reboots by default. Recreating
# here keeps each VM's network self-contained. The first VM unit to start after
# a host reboot rebuilds both the v6 forward chain and the v4 egress NAT.
sudo nft list table inet atlas >/dev/null 2>&1 || sudo nft add table inet atlas
sudo nft list chain inet atlas forward >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas forward { type filter hook forward priority filter; policy accept; }"
sudo nft list chain inet atlas postrouting >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas postrouting { type nat hook postrouting priority srcnat; policy accept; }"
sudo nft list chain inet atlas postrouting | grep -q "ip saddr 100.64.0.0/16" || \
    sudo nft add rule inet atlas postrouting ip saddr 100.64.0.0/16 oifname "$ipv4_uplink" masquerade

# Sysctls cleared on reboot if not persisted via /etc/sysctl.d. Bootstrap
# writes /etc/sysctl.d/60-atlas.conf, but a defensive re-apply costs nothing.
sudo sysctl -q -w net.ipv6.conf.all.forwarding=1 net.ipv6.conf.all.proxy_ndp=1 net.ipv4.ip_forward=1 || true

# Tap device: clean re-create so a restart picks up correct state.
sudo ip link del "$TAP_DEVICE" 2>/dev/null || true
sudo ip tuntap add "$TAP_DEVICE" mode tap vnet_hdr
sudo ip link set "$TAP_DEVICE" up
sudo ip -6 addr add fe80::1/64 dev "$TAP_DEVICE" nodad
# Host side of the per-VM /30 NAT44 link. The guest uses this address as its
# IPv4 default gateway; the connected route the /30 creates reaches the guest,
# so no explicit per-VM v4 route is needed.
sudo ip -4 addr replace "$IPV4_HOST_CIDR" dev "$TAP_DEVICE"

# Route the VM's /128 over the tap.
sudo ip -6 route replace "${VIRTUAL_MACHINE_IPV6}/128" dev "$TAP_DEVICE"

# Answer NDP for the VM on the uplink.
sudo ip -6 neigh replace proxy "$VIRTUAL_MACHINE_IPV6" dev "$uplink"

# Forwarding rules.
sudo nft add rule inet atlas forward ip6 daddr "$VIRTUAL_MACHINE_IPV6" oifname "$TAP_DEVICE" accept
sudo nft add rule inet atlas forward ip6 saddr "$VIRTUAL_MACHINE_IPV6" iifname "$TAP_DEVICE" accept
