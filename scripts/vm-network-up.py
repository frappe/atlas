#!/usr/bin/env python3
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
#
# systemd-invoked, NOT a Task: it takes a single positional argument (the VM
# UUID), not --flags, because the unit's ExecStartPre passes `%i`. It imports the
# DURABLE atlas package under /var/lib/atlas/bin (placed by bootstrap), not the
# per-task staged copy.

import os
import sys

# The durable package lives next to this script under /var/lib/atlas/bin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atlas._run import run, run_ok
from atlas.firewall import apply_persisted_firewall
from atlas.network_env import default_route_device, read_network_env
from atlas.networkd.localownership import add_local_owned
from atlas.paths import VirtualMachinePaths
from atlas.private_network import apply_private_network
from atlas.reserved_ip_nat import (
	apply_reserved_ip_nat,
	apply_routed_reserved_ip_nat,
	discover_reserved_ip_anchor,
)
from atlas.wireguard import apply_persisted_tunnels


def main() -> None:
	if len(sys.argv) != 2:
		sys.exit("usage: vm-network-up.py <virtual-machine-uuid>")
	uuid = sys.argv[1]

	env = read_network_env(VirtualMachinePaths(uuid).network_env)
	tap_device = env.require("TAP_DEVICE")
	virtual_machine_ipv6 = env.require("VIRTUAL_MACHINE_IPV6")
	atlas_netns = env.require("ATLAS_NETNS")
	host_veth = env.require("HOST_VETH")
	namespace_veth = env.require("NAMESPACE_VETH")
	ipv4_host_cidr = env.require("IPV4_HOST_CIDR")
	ipv4_guest_cidr = env.require("IPV4_GUEST_CIDR")
	# Optional: a Reserved IP attached to this VM. Present only for the VMs that
	# opted into inbound v4 (today, the reverse proxy). Absent for every ordinary
	# VM, so the 1:1-NAT block below is skipped and nothing changes.
	reserved_ipv4 = env.get("RESERVED_IPV4")

	# Optional: the VM's private-plane identity on the WireGuard host mesh (design
	# §5). Present once the controller writes PRIVATE_ADDRESS + TENANT_PREFIX into
	# network.env (universal private addressing). Absent on a pre-feature VM, so the
	# private-plane block below is skipped and the VM keeps exactly today's behavior.
	private_address = env.get("PRIVATE_ADDRESS")
	tenant_prefix = env.get("TENANT_PREFIX")

	# The guest's private v4 as a bare host address. The host routes a /32 to it
	# (the v4 analog of the VM's /128 v6 route) — a route prefix must be a network
	# address, so we cannot reuse IPV4_HOST_CIDR (a /30 carrying a host address;
	# `ip route` rejects "100.64.x.9/30" as an invalid prefix).
	ipv4_guest_address = ipv4_guest_cidr.split("/", 1)[0]

	uplink = default_route_device("-6")
	# The default-route dev for v4 egress (may differ from the v6 uplink on a
	# multi-homed host); used for the masquerade rule.
	ipv4_uplink = default_route_device()

	# Idempotent nftables scaffold. The bootstrap script creates these on first
	# install, but they're not persisted across host reboots by default. Recreating
	# here keeps each VM's network self-contained. The first VM unit to start after
	# a host reboot rebuilds both the v6 forward chain and the v4 egress NAT.
	if not run_ok("sudo nft list table inet atlas"):
		run("sudo nft add table inet atlas")
	if not run_ok("sudo nft list chain inet atlas forward"):
		run(
			"sudo nft add chain inet atlas forward {}",
			"{ type filter hook forward priority filter; policy accept; }",
		)
	# Re-assert the host IMDS-drop (bootstrap's 9-imds): a guest must never reach the
	# host's metadata endpoint (169.254.169.254 serves the droplet's own vendor
	# credentials). Recreated here so the first VM to start after a host reboot
	# rebuilds it, like the masquerade rule. The guest's own MMDS is served by
	# Firecracker on the tap inside the netns and never crosses this chain.
	if "ip daddr 169.254.169.254" not in run("sudo nft list chain inet atlas forward"):
		run("sudo nft add rule inet atlas forward ip daddr 169.254.169.254 drop")
	if not run_ok("sudo nft list chain inet atlas postrouting"):
		run(
			"sudo nft add chain inet atlas postrouting {}",
			"{ type nat hook postrouting priority srcnat; policy accept; }",
		)
	postrouting = run("sudo nft list chain inet atlas postrouting")
	if "ip saddr 100.64.0.0/16" not in postrouting:
		run(
			"sudo nft add rule inet atlas postrouting ip saddr 100.64.0.0/16 oifname {} masquerade",
			ipv4_uplink,
		)

	# Sysctls cleared on reboot if not persisted via /etc/sysctl.d. Bootstrap writes
	# /etc/sysctl.d/60-atlas.conf, but a defensive re-apply costs nothing. Forwarding
	# (v6 and v4) now also carries traffic across the veth seam.
	run(
		"sudo sysctl -q -w net.ipv6.conf.all.forwarding=1 net.ipv6.conf.all.proxy_ndp=1 net.ipv4.ip_forward=1",
		check=False,
	)

	# 1. Network namespace. Clean re-create so a restart starts from a known state
	#    (deleting the namespace takes its tap + the namespace-side veth with it).
	run("sudo ip netns del {}", atlas_netns, check=False)
	run("sudo ip link del {}", host_veth, check=False)
	run("sudo ip netns add {}", atlas_netns)

	# 2. veth pair: one end stays on the host, the other moves into the namespace.
	run("sudo ip link add {} type veth peer name {}", host_veth, namespace_veth)
	run("sudo ip link set {} netns {}", namespace_veth, atlas_netns)

	# 3. The namespace forwards between the veth (uplink side) and the tap (guest
	#    side) for both families, so it needs its own forwarding sysctls —
	#    namespaces have independent network sysctls and default to forwarding off.
	run(
		"sudo ip netns exec {} sysctl -q -w net.ipv6.conf.all.forwarding=1 net.ipv4.ip_forward=1",
		atlas_netns,
		check=False,
	)

	# 4. Tap inside the namespace. vnet_hdr matches what Firecracker expects; fe80::1
	#    is the guest's IPv6 gateway and IPV4_HOST_CIDR (the host side of the per-VM
	#    NAT44 /30) is its IPv4 gateway — both unchanged guest contracts, just moved
	#    inside the namespace. Route the VM's /128 to the tap so replies reach the
	#    guest; the v4 /30 is reached by its connected route.
	run(
		"sudo ip netns exec {} ip tuntap add {} mode tap vnet_hdr",
		atlas_netns,
		tap_device,
	)
	run("sudo ip netns exec {} ip link set {} up", atlas_netns, tap_device)
	run(
		"sudo ip netns exec {} ip -6 addr add fe80::1/64 dev {} nodad",
		atlas_netns,
		tap_device,
	)
	run(
		"sudo ip netns exec {} ip -6 route replace {} dev {}",
		atlas_netns,
		f"{virtual_machine_ipv6}/128",
		tap_device,
	)
	run(
		"sudo ip netns exec {} ip -4 addr replace {} dev {}",
		atlas_netns,
		ipv4_host_cidr,
		tap_device,
	)

	# 5. Bring up both ends of the veth and address it for transit. IPv6 uses
	#    fe80::2 (host) / fe80::3 (ns); IPv4 uses a link-local /30 (169.254.0.0/30)
	#    that exists only on this veth pair (isolated per namespace), purely to carry
	#    the guest's masqueraded v4 to the host. The namespace's default routes for
	#    both families point at the host end.
	run("sudo ip link set {} up", host_veth)
	run("sudo ip -6 addr add fe80::2/64 dev {} nodad", host_veth)
	run("sudo ip -4 addr replace 169.254.0.1/30 dev {}", host_veth)
	run("sudo ip netns exec {} ip link set {} up", atlas_netns, namespace_veth)
	run(
		"sudo ip netns exec {} ip -6 addr add fe80::3/64 dev {} nodad",
		atlas_netns,
		namespace_veth,
	)
	run(
		"sudo ip netns exec {} ip -4 addr replace 169.254.0.2/30 dev {}",
		atlas_netns,
		namespace_veth,
	)
	run(
		"sudo ip netns exec {} ip -6 route replace default via fe80::2 dev {}",
		atlas_netns,
		namespace_veth,
	)
	run(
		"sudo ip netns exec {} ip -4 route replace default via 169.254.0.1 dev {}",
		atlas_netns,
		namespace_veth,
	)

	# 6. On the host: route the VM's /128 (v6) and its private /30 (v4) into the
	#    namespace via the veth, and answer NDP for the VM on the uplink so the
	#    upstream router delivers its v6 packets here. The v4 return path relies on
	#    the masquerade conntrack, but the explicit /30 route lets the host reach the
	#    guest's private v4 directly too.
	run(
		"sudo ip -6 route replace {} via fe80::3 dev {}",
		f"{virtual_machine_ipv6}/128",
		host_veth,
	)
	run("sudo ip -6 neigh replace proxy {} dev {}", virtual_machine_ipv6, uplink)
	run(
		"sudo ip -4 route replace {} via 169.254.0.2 dev {}",
		f"{ipv4_guest_address}/32",
		host_veth,
	)

	# 7. Forwarding rules, matching the host-side veth (the tap is no longer in the
	#    host namespace to match on). The v4 masquerade rule (host postrouting,
	#    100.64.0.0/16 -> uplink) is created in the nft scaffold above and covers the
	#    guest's v4 egress once it reaches the host via the veth.
	run(
		"sudo nft add rule inet atlas forward ip6 daddr {} oifname {} accept",
		virtual_machine_ipv6,
		host_veth,
	)
	run(
		"sudo nft add rule inet atlas forward ip6 saddr {} iifname {} accept",
		virtual_machine_ipv6,
		host_veth,
	)

	# 7a. Private plane (the WireGuard host mesh, design §5). Present only once the
	#     controller writes PRIVATE_ADDRESS + TENANT_PREFIX into network.env; absent
	#     on a pre-feature VM, so this whole block is skipped and the VM keeps exactly
	#     today's public-only behavior. When present:
	#       - route the VM's private /128 to the tap (inside the netns) so a packet
	#         decap'd from wg-mesh reaches the guest, and to the host veth (host ns) so
	#         the host forwards it in — siblings of the public /128 lines in step 4/6.
	#         There is NO proxy-NDP for fdaa:: — it is never on-link (it is routed out
	#         wg-mesh by the host-mesh route, not delivered on the uplink).
	#       - install the per-VM isolation rules + re-assert the terminal default-deny
	#         (apply_private_network, design §4): anti-spoof, same-tenant egress, infra
	#         reach-through, cross-host delivery. Idempotent (guarded on canonical nft
	#         text), so a cold boot / restart is a no-op.
	if private_address and tenant_prefix:
		run(
			"sudo ip netns exec {} ip -6 route replace {} dev {}",
			atlas_netns,
			f"{private_address}/128",
			tap_device,
		)
		run(
			"sudo ip -6 route replace {} via fe80::3 dev {}",
			f"{private_address}/128",
			host_veth,
		)
		apply_private_network(host_veth, private_address, tenant_prefix)
		# Record the VM's private /128 in the local-ownership cache that
		# atlas-networkd's scan (spec/31 §11) picks up on its next tick — the
		# daemon then gossips the advertisement fleet-wide.
		if private_address:
			add_local_owned(private_address)

	# 8. Inbound v4: if a Reserved IP is attached, 1:1-NAT it to the guest's /30,
	#    rebuilt on every cold boot from the RESERVED_IPV4 flag like the scaffold
	#    above. Two delivery models, discovered fresh on the host:
	#    - DigitalOcean binds the IP via an ANCHOR (the destination DO actually
	#      delivers reserved-IP traffic to — NOT the reserved IP, which never appears
	#      on the droplet): DNAT the anchor in, SNAT out as the anchor + a policy
	#      route via the anchor gateway so DO maps it back to the reserved IP.
	#    - A routed flexible IP (Self-Managed / Scaleway Elastic Metal) arrives at
	#      the host destined to the reserved IP itself: DNAT it directly, no anchor.
	#    The guest stays unaware (sees only its private v4). See reserved_ip_nat.py
	#    and the atlas-reserved-ip-anchor-dnat finding.
	if reserved_ipv4:
		anchor = discover_reserved_ip_anchor()
		if anchor is not None:
			apply_reserved_ip_nat(anchor, ipv4_guest_address, host_veth)
		else:
			apply_routed_reserved_ip_nat(reserved_ipv4, ipv4_guest_address, host_veth)

	# 9. Re-apply any persisted WireGuard tunnels (spec/19-vpn-broker.md). Each
	#    terminates in this (host root) netns and routes to the VM's /128 via the
	#    host veth, so it comes up functional only now that route exists. A VM with
	#    no tunnels has no tunnels/ dir — a no-op.
	apply_persisted_tunnels(VirtualMachinePaths(uuid).tunnels_directory)

	# 10. Re-apply this VM's public-ingress firewall (spec/20-firewall.md), if one is
	#     attached. Done last, after the VM's /128 route exists, so the public_filter
	#     drop and the broad forward accept are both in place. No firewall.env (no
	#     firewall attached) is a no-op — the VM stays fully public.
	apply_persisted_firewall(VirtualMachinePaths(uuid).firewall_env)


if __name__ == "__main__":
	main()
