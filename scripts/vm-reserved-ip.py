#!/usr/bin/env python3
# Attach or detach a Reserved IP's host-side 1:1-NAT to a RUNNING VM, with no
# reboot. The inbound mirror of NAT44 egress: the reserved v4 lands on the
# droplet (DO binds it via an anchor IP, not a routed prefix — so unlike the VM's
# public v6 it cannot be routed straight to the guest), and the host DNATs it in
# and SNATs the guest's egress out as the reserved IP. See spec/06-networking.md
# and lib/atlas/reserved_ip_nat.py for why NAT, not routing, is forced here.
#
# Two effects, both idempotent:
#   1. Mutate network.env (add/remove RESERVED_IPV4) so a later cold boot re-
#      creates the same NAT from disk via vm-network-up.py — the env is the
#      durable source of truth for the host networking, as for every other VM.
#   2. Apply/remove the live nft rules now, so an attach takes effect without
#      waiting for a restart.
#
# Unlike vm-network-up/down (systemd %i, positional arg), this is a Task: it
# takes typed --flags parsed by ReservedIpInputs.from_args(), dispatched by
# Reserved IP.attach()/detach(). It imports the per-task staged atlas package
# under /tmp/atlas/lib, like the other Task scripts.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_file
from atlas._task import TaskInputs
from atlas.network_env import read_network_env, remove_network_env, upsert_network_env
from atlas.paths import VirtualMachinePaths
from atlas.reserved_ip_nat import (
	apply_reserved_ip_nat,
	apply_routed_reserved_ip_nat,
	discover_reserved_ip_anchor,
	remove_reserved_ip_nat,
)


@dataclass(frozen=True)
class ReservedIpInputs(TaskInputs):
	"""Attach or detach a Reserved IP's host 1:1-NAT to a running VM."""

	command: typing.ClassVar[str] = "vm-reserved-ip"
	virtual_machine_name: str  # UUID; locates the VM directory + network.env
	reserved_ipv4: str  # the public v4 to 1:1-NAT (attach) or tear down (detach)
	action: str = "attach"  # "attach" | "detach"


def main() -> None:
	inputs = ReservedIpInputs.from_args()
	if inputs.action not in ("attach", "detach"):
		sys.exit(f"action must be attach|detach, got {inputs.action!r}")

	paths = VirtualMachinePaths(inputs.virtual_machine_name)
	env = read_network_env(paths.network_env)
	# The guest's private v4 (host side of its /30 is the gateway) — the NAT target.
	# network.env always carries it; the bare address is the DNAT destination and
	# the SNAT source, mirroring vm-network-up.py.
	guest_ipv4 = env.require("IPV4_GUEST_CIDR").split("/", 1)[0]
	host_veth = env.require("HOST_VETH")

	# nosemgrep: frappe-security-file-traversal -- host script; reads the per-VM network.env path derived from the VM name, not untrusted web input
	with open(paths.network_env) as handle:
		current = handle.read()

	if inputs.action == "attach":
		# RESERVED_IPV4 in network.env is the durable "this VM has inbound v4" flag
		# (the public identity), re-applied at every cold boot (vm-network-up).
		# Delivery model is discovered fresh on the host: DigitalOcean hands the IP
		# over via an anchor (DNAT the anchor, egress via its gateway); a routed
		# flexible IP (Self-Managed / Scaleway Elastic Metal) arrives at the host
		# destined to the reserved IP itself, so DNAT it directly with no anchor.
		anchor = discover_reserved_ip_anchor()
		updated = upsert_network_env(current, "RESERVED_IPV4", inputs.reserved_ipv4)
		install_file(updated, paths.network_env, mode="0644")
		if anchor is not None:
			apply_reserved_ip_nat(anchor, guest_ipv4, host_veth)
			print(
				f"Attached {inputs.reserved_ipv4} (anchor {anchor.address} via {anchor.gateway}) "
				f"-> {guest_ipv4} on {inputs.virtual_machine_name}."
			)
		else:
			apply_routed_reserved_ip_nat(inputs.reserved_ipv4, guest_ipv4, host_veth)
			print(
				f"Attached {inputs.reserved_ipv4} (routed) -> {guest_ipv4} on {inputs.virtual_machine_name}."
			)
	else:
		updated = remove_network_env(current, "RESERVED_IPV4")
		install_file(updated, paths.network_env, mode="0644")
		remove_reserved_ip_nat(guest_ipv4)
		print(f"Detached {inputs.reserved_ipv4} from {inputs.virtual_machine_name}.")


if __name__ == "__main__":
	main()
