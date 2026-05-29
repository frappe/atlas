#!/bin/bash
# Host-side network for a VM. Invoked by ExecStartPre in the systemd unit
# (must run before the jailer's ExecStart so the namespace + tap exist when the
# jailer joins the netns and Firecracker opens the tap). Reads
# /var/lib/atlas/virtual-machines/$1/network.env. Idempotent.
#
# Approach: each VM gets its OWN network namespace so a jail breakout cannot see
# the host's interfaces, the uplink, or other VMs' taps. The VM's tap lives
# inside that namespace; a veth pair bridges the namespace back to the host.
#
# The server has DigitalOcean's /64 prefix routed to it, but only a /124 is
# *usable* (DO routes the /64 to the droplet; the rest has no route inside DO's
# fabric). So we hand out addresses inside a fixed /124 and use proxy-NDP on the
# uplink to make the upstream router believe each VM address is on-link. The
# guest still uses fe80::1 (on the tap, inside its namespace) as its gateway;
# the only change from the host-netns model is one extra link-local hop across
# the veth, fully inside the host.

set -euo pipefail

virtual_machine_name="${1:?virtual machine name required}"
. "/var/lib/atlas/virtual-machines/${virtual_machine_name}/network.env"

: "${TAP_DEVICE:?missing in network.env}"
: "${VIRTUAL_MACHINE_IPV6:?missing in network.env}"
: "${ATLAS_NETNS:?missing in network.env}"
: "${HOST_VETH:?missing in network.env}"
: "${NAMESPACE_VETH:?missing in network.env}"
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

# Sysctls cleared on reboot if not persisted via /etc/sysctl.d. Bootstrap writes
# /etc/sysctl.d/60-atlas.conf, but a defensive re-apply costs nothing. Forwarding
# (v6 and v4) now also carries traffic across the veth seam.
sudo sysctl -q -w net.ipv6.conf.all.forwarding=1 net.ipv6.conf.all.proxy_ndp=1 net.ipv4.ip_forward=1 || true

# 1. Network namespace. Clean re-create so a restart starts from a known state
#    (deleting the namespace takes its tap + the namespace-side veth with it).
sudo ip netns del "$ATLAS_NETNS" 2>/dev/null || true
sudo ip link del "$HOST_VETH" 2>/dev/null || true
sudo ip netns add "$ATLAS_NETNS"

# 2. veth pair: one end stays on the host, the other moves into the namespace.
sudo ip link add "$HOST_VETH" type veth peer name "$NAMESPACE_VETH"
sudo ip link set "$NAMESPACE_VETH" netns "$ATLAS_NETNS"

# 3. The namespace forwards between the veth (uplink side) and the tap (guest
#    side) for both families, so it needs its own forwarding sysctls —
#    namespaces have independent network sysctls and default to forwarding off.
sudo ip netns exec "$ATLAS_NETNS" sysctl -q -w net.ipv6.conf.all.forwarding=1 net.ipv4.ip_forward=1 || true

# 4. Tap inside the namespace. vnet_hdr matches what Firecracker expects; fe80::1
#    is the guest's IPv6 gateway and IPV4_HOST_CIDR (the host side of the per-VM
#    NAT44 /30) is its IPv4 gateway — both unchanged guest contracts, just moved
#    inside the namespace. Route the VM's /128 to the tap so replies reach the
#    guest; the v4 /30 is reached by its connected route.
sudo ip netns exec "$ATLAS_NETNS" ip tuntap add "$TAP_DEVICE" mode tap vnet_hdr
sudo ip netns exec "$ATLAS_NETNS" ip link set "$TAP_DEVICE" up
sudo ip netns exec "$ATLAS_NETNS" ip -6 addr add fe80::1/64 dev "$TAP_DEVICE" nodad
sudo ip netns exec "$ATLAS_NETNS" ip -6 route replace "${VIRTUAL_MACHINE_IPV6}/128" dev "$TAP_DEVICE"
sudo ip netns exec "$ATLAS_NETNS" ip -4 addr replace "$IPV4_HOST_CIDR" dev "$TAP_DEVICE"

# 5. Bring up both ends of the veth and address it for transit. IPv6 uses
#    fe80::2 (host) / fe80::3 (ns); IPv4 uses a link-local /30 (169.254.0.0/30)
#    that exists only on this veth pair (isolated per namespace), purely to carry
#    the guest's masqueraded v4 to the host. The namespace's default routes for
#    both families point at the host end.
sudo ip link set "$HOST_VETH" up
sudo ip -6 addr add fe80::2/64 dev "$HOST_VETH" nodad
sudo ip -4 addr replace 169.254.0.1/30 dev "$HOST_VETH"
sudo ip netns exec "$ATLAS_NETNS" ip link set "$NAMESPACE_VETH" up
sudo ip netns exec "$ATLAS_NETNS" ip -6 addr add fe80::3/64 dev "$NAMESPACE_VETH" nodad
sudo ip netns exec "$ATLAS_NETNS" ip -4 addr replace 169.254.0.2/30 dev "$NAMESPACE_VETH"
sudo ip netns exec "$ATLAS_NETNS" ip -6 route replace default via fe80::2 dev "$NAMESPACE_VETH"
sudo ip netns exec "$ATLAS_NETNS" ip -4 route replace default via 169.254.0.1 dev "$NAMESPACE_VETH"

# 6. On the host: route the VM's /128 (v6) and its private /30 (v4) into the
#    namespace via the veth, and answer NDP for the VM on the uplink so the
#    upstream router delivers its v6 packets here. The v4 return path relies on
#    the masquerade conntrack, but the explicit /30 route lets the host reach the
#    guest's private v4 directly too.
sudo ip -6 route replace "${VIRTUAL_MACHINE_IPV6}/128" via fe80::3 dev "$HOST_VETH"
sudo ip -6 neigh replace proxy "$VIRTUAL_MACHINE_IPV6" dev "$uplink"
sudo ip -4 route replace "$IPV4_HOST_CIDR" via 169.254.0.2 dev "$HOST_VETH"

# 7. Forwarding rules, matching the host-side veth (the tap is no longer in the
#    host namespace to match on). The v4 masquerade rule (host postrouting,
#    100.64.0.0/16 -> uplink) is created in the nft scaffold above and covers the
#    guest's v4 egress once it reaches the host via the veth.
sudo nft add rule inet atlas forward ip6 daddr "$VIRTUAL_MACHINE_IPV6" oifname "$HOST_VETH" accept
sudo nft add rule inet atlas forward ip6 saddr "$VIRTUAL_MACHINE_IPV6" iifname "$HOST_VETH" accept
