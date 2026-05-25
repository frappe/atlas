#!/bin/bash
# Host-side network for a VM. Invoked by ExecStartPost in the systemd unit.
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

uplink="$(ip -j -6 route show default | jq -r '.[0].dev')"

# Idempotent nftables scaffold. The bootstrap script creates these on first
# install, but they're not persisted across host reboots by default. Recreating
# here keeps each VM's network self-contained.
sudo nft list table inet atlas >/dev/null 2>&1 || sudo nft add table inet atlas
sudo nft list chain inet atlas forward >/dev/null 2>&1 || \
    sudo nft "add chain inet atlas forward { type filter hook forward priority filter; policy accept; }"

# Sysctls cleared on reboot if not persisted via /etc/sysctl.d. Bootstrap
# writes /etc/sysctl.d/60-atlas.conf, but a defensive re-apply costs nothing.
sudo sysctl -q -w net.ipv6.conf.all.forwarding=1 net.ipv6.conf.all.proxy_ndp=1 || true

# Tap device: clean re-create so a restart picks up correct state.
sudo ip link del "$TAP_DEVICE" 2>/dev/null || true
sudo ip tuntap add "$TAP_DEVICE" mode tap
sudo ip link set "$TAP_DEVICE" up
sudo ip -6 addr add fe80::1/64 dev "$TAP_DEVICE" nodad

# Route the VM's /128 over the tap.
sudo ip -6 route replace "${VIRTUAL_MACHINE_IPV6}/128" dev "$TAP_DEVICE"

# Answer NDP for the VM on the uplink.
sudo ip -6 neigh replace proxy "$VIRTUAL_MACHINE_IPV6" dev "$uplink"

# Forwarding rules.
sudo nft add rule inet atlas forward ip6 daddr "$VIRTUAL_MACHINE_IPV6" oifname "$TAP_DEVICE" accept
sudo nft add rule inet atlas forward ip6 saddr "$VIRTUAL_MACHINE_IPV6" iifname "$TAP_DEVICE" accept
