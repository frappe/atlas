#!/usr/bin/env python3
# Source side of a keep-address migration's cutover (spec/24 §2.2, §2.9.2): point
# the VM's /128 delivery at the forward tunnel instead of its now-torn-down veth.
#
# By the time this runs, the source VM's unit is disabled and its ExecStopPost
# (vm-network-down.py) has already deleted this VM's netns/veth/tap AND its
# `<vmv6>/128 via fe80::3 dev <host_veth>` route — and, on a proxy-NDP provider,
# its proxy-NDP entry. But the source host still holds the /64, so inbound for the
# /128 still lands here. This re-establishes reachability onto the tunnel:
#
#   ip -6 route replace <vmv6>/128 dev <tunnel>     (atomic; no black hole)
#   nft ... daddr <vmv6> oifname <tunnel> accept     (forward inbound to the tunnel)
#   nft ... saddr <vmv6> iifname <tunnel> accept     (admit the reply coming back)
#
# On a proxy-NDP provider (DigitalOcean, forward_address=1) it ALSO re-asserts the
# proxy-NDP entry the unit stop just removed, so DO's edge keeps delivering the
# /128 here. On a routed-prefix provider (Scaleway) proxy-NDP is a no-op, so it is
# skipped — the routed /64 already delivers the /128 to this host.
#
# This is the point the forward becomes live and PERMANENT: nothing tears it down
# automatically (spec/24 §2.9.4). The operator collapses it by hand later
# (migration-forward-down.py via the Collapse-forward action).
#
# Idempotent: `ip route replace` and duplicate-guarded nft adds re-assert cleanly.
#
# Inputs:
#   virtual_machine_name  - UUID (for logging / symmetry)
#   virtual_machine_ipv6  - the /128 being forwarded (unchanged across the move)
#   tunnel_device         - the mig6-<vm8> interface (already up from forward-up)
#   reassert_proxy_ndp    - "1" on a proxy-NDP provider (DO), "0" on Scaleway

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.network_env import default_route_device


@dataclass(frozen=True)
class SourceForwardInputs(TaskInputs):
	"""Repoint the source's /128 delivery onto the forward tunnel at cutover."""

	command: typing.ClassVar[str] = "migration-source-forward"
	virtual_machine_name: str
	virtual_machine_ipv6: str
	tunnel_device: str
	reassert_proxy_ndp: str = "0"


@dataclass(frozen=True)
class SourceForwardResult(TaskResult):
	forwarding: bool = True


def main() -> None:
	inputs = SourceForwardInputs.from_args()
	vmv6 = inputs.virtual_machine_ipv6

	if not run_ok("ip link show {}", inputs.tunnel_device):
		sys.exit(f"forward tunnel {inputs.tunnel_device} is not up; run migration-forward-up first")

	# 1. Route the /128 into the tunnel. Atomic replace (single rtnetlink op) — no
	#    delete-then-add black hole. vm-network-down already removed the competing
	#    same-length veth route, so there is no specificity contest.
	run("sudo ip -6 route replace {} dev {}", f"{vmv6}/128", inputs.tunnel_device)

	# 2. Forward chain: admit inbound toward the tunnel and the reply coming back.
	#    The `inet atlas forward` chain is created by bootstrap / vm-network-up; a
	#    migration source has hosted VMs, so it exists. Guard each add against a
	#    re-entry duplicate by checking the rendered match is not already present.
	_ensure_forward_rule(f"ip6 daddr {vmv6} oifname {inputs.tunnel_device} accept")
	_ensure_forward_rule(f"ip6 saddr {vmv6} iifname {inputs.tunnel_device} accept")

	# 3. Proxy-NDP re-assert (DigitalOcean only). On a routed prefix (Scaleway) this
	#    is a no-op at the edge, so we skip it to keep the source's neigh table clean.
	if inputs.reassert_proxy_ndp == "1":
		uplink = default_route_device("-6")
		run("sudo ip -6 neigh replace proxy {} dev {}", vmv6, uplink)

	SourceForwardResult().emit()
	print(f"Source now forwarding {vmv6} over {inputs.tunnel_device}.")


def _ensure_forward_rule(match: str) -> None:
	"""Add a rule to `inet atlas forward` unless an identical match already exists.
	Mirrors vm-network-up.py's substring guard on the listed chain — nft has no
	native 'add if absent', so we list and check."""
	chain = run("sudo nft list chain inet atlas forward", check=False)
	if match in chain:
		return
	# The match is composed from a validated /128 and a derived device name (no
	# untrusted input), so it inlines into the rule.
	run(f"sudo nft add rule inet atlas forward {match}")


if __name__ == "__main__":
	main()
