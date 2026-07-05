#!/usr/bin/env python3
# Symmetric teardown for vm-network-up.py. Invoked by ExecStopPost on the
# systemd unit. Idempotent: missing rules, devices and namespaces are not an
# error.
#
# systemd-invoked, NOT a Task: it takes a single positional argument (the VM
# UUID), not --flags, because the unit's ExecStopPost passes `%i`. It imports the
# DURABLE atlas package under /var/lib/atlas/bin (placed by bootstrap), not the
# per-task staged copy.

import os
import sys

# The durable package lives next to this script under /var/lib/atlas/bin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atlas._run import run
from atlas.firewall import remove_firewall
from atlas.network_env import default_route_device, read_network_env_optional
from atlas.paths import VirtualMachinePaths
from atlas.private_network import remove_private_network
from atlas.reserved_ip_nat import remove_reserved_ip_nat


def main() -> None:
	if len(sys.argv) != 2:
		sys.exit("usage: vm-network-down.py <virtual-machine-uuid>")
	uuid = sys.argv[1]

	paths = VirtualMachinePaths(uuid)

	# If the env file is gone (terminate-vm already ran) we still want to do our
	# best to clean up. read_network_env_optional() returns an empty NetworkEnv
	# when the file is absent — so unlike the disk/up hooks we never raise on a
	# missing env. Every value is read with .get() (the `${VAR:-}` form) and each
	# step guarded by `if value:` (the `[ -n ]` form).
	env = read_network_env_optional(paths.network_env)

	virtual_machine_ipv6 = env.get("VIRTUAL_MACHINE_IPV6")
	host_veth = env.get("HOST_VETH")
	ipv4_guest_cidr = env.get("IPV4_GUEST_CIDR")
	atlas_netns = env.get("ATLAS_NETNS")
	reserved_ipv4 = env.get("RESERVED_IPV4")
	# The VM's private-plane /128 (design §5). Present once the controller writes it;
	# absent on a pre-feature or dark-less VM, so the private teardown below no-ops.
	private_address = env.get("PRIVATE_ADDRESS")

	# Drop the inbound-v4 1:1-NAT first, while we still have the guest /30 from
	# the env (the namespace delete below would otherwise leave the host-table
	# rules + policy route dangling). Keyed on the guest v4 alone — no anchor
	# rediscovery needed. Best-effort, like everything in this teardown.
	if reserved_ipv4 and ipv4_guest_cidr:
		remove_reserved_ip_nat(ipv4_guest_cidr.split("/", 1)[0])

	# The v6 uplink for the proxy-NDP delete. The shell's trailing `|| true`
	# tolerates a missing default route — default_route_device(tolerate_missing).
	uplink = default_route_device("-6", tolerate_missing=True)

	# Proxy-NDP entry on the uplink.
	if virtual_machine_ipv6 and uplink:
		run("sudo ip -6 neigh del proxy {} dev {}", virtual_machine_ipv6, uplink, check=False)

	# Host-side routes into the namespace (v6 /128 and the guest's v4 /32). The
	# tap, its IPv4 /30 host address, and the namespace-side veth all live inside
	# the namespace, so deleting the namespace drops them in one go — no per-device
	# v4 teardown needed. The masquerade rule is host-wide (matches the whole
	# 100.64.0.0/16 source), so it is intentionally NOT removed per-VM — it stays
	# for the next VM, exactly like the v6 forward chain scaffold.
	if host_veth:
		if virtual_machine_ipv6:
			run("sudo ip -6 route del {} dev {}", f"{virtual_machine_ipv6}/128", host_veth, check=False)
		if private_address:
			# The private-plane /128 host route (design §5). Its netns-side sibling (the
			# route to the tap) goes with the namespace delete below, like the public
			# /128's does. No proxy-NDP to delete — fdaa:: was never on-link.
			run("sudo ip -6 route del {} dev {}", f"{private_address}/128", host_veth, check=False)
		if ipv4_guest_cidr:
			# ${IPV4_GUEST_CIDR%/*}/32 — strip the original prefix, route the /32.
			guest_v4 = ipv4_guest_cidr.split("/", 1)[0]
			run("sudo ip -4 route del {} dev {}", f"{guest_v4}/32", host_veth, check=False)

	# The namespace owns the tap and the namespace-side veth; deleting it takes both.
	if atlas_netns:
		run("sudo ip netns del {}", atlas_netns, check=False)

	# The host-side veth end (its peer went with the namespace, but delete defensively).
	if host_veth:
		run("sudo ip link del {}", host_veth, check=False)

	# Delete the two PUBLIC nft rules by handle. Look them up by VM IPv6.
	if virtual_machine_ipv6:
		# handles="$(sudo nft -a list chain inet atlas forward 2>/dev/null \
		#     | awk -v ip="$VIRTUAL_MACHINE_IPV6" '$0 ~ ip {print $NF}')"
		# List the chain (tolerate absence), then in Python find every rule line
		# mentioning this VM's IPv6 and take its trailing handle number.
		chain = run("sudo nft -a list chain inet atlas forward", check=False)
		handles = []
		for line in chain.splitlines():
			if virtual_machine_ipv6 in line:
				handles.append(line.split()[-1])
		for handle in handles:
			run("sudo nft delete rule inet atlas forward handle {}", handle, check=False)

		# The public-ingress firewall block lives in its own higher-priority chain
		# (spec/20-firewall.md), in the host root netns — the namespace delete above
		# does not touch it. Remove it too, so a future VM that reuses this IPv6 is not
		# silently blocked by a stale drop. The firewall.env sidecar (in the VM dir) is
		# swept by terminate's rm -rf; on a plain stop it persists and vm-network-up
		# re-applies the block on the next start. Best-effort, idempotent.
		remove_firewall(virtual_machine_ipv6)

	# Delete the PRIVATE-plane isolation rules (design §4/§5). This is the teardown-bug
	# fix: the public sweep above matches only VIRTUAL_MACHINE_IPV6, and the private
	# rules are keyed on the PRIVATE /128 + the veth — so a public-only sweep would
	# NEVER remove them, leaving stale rules pointing at a deleted (and potentially
	# recycled) veth = a cross-tenant leak. Runs INDEPENDENTLY of virtual_machine_ipv6
	# because a dark VM (public_networking=0) has NO public /128 at all, so a
	# public-gated sweep would be a complete no-op for it. The host-wide terminal
	# `fdaa::/16 drop` mentions neither this /128 nor the veth, so it is left in place
	# for the next VM (like the masquerade / IMDS scaffold). Best-effort, idempotent.
	if private_address and host_veth:
		remove_private_network(private_address, host_veth)


if __name__ == "__main__":
	main()
