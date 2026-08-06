#!/usr/bin/env python3
# Tear down one end of a VM migration's keep-address forward tunnel (spec/24
# §2.9.5, the Collapse-forward action). The inverse of migration-forward-up plus
# migration-source-forward (source) / migration-target-receive (target).
#
# The keep-address forward is PERMANENT by default — nothing collapses it
# automatically. The operator invokes Collapse-forward once they are ready (the
# source really is going away, or they want to drop the extra hop); the migration
# controller then runs this on BOTH hosts and falls the VM back to change-address
# semantics (a new /128, proxy re-point) in the same action.
#
# Best-effort and idempotent throughout, like vm-network-down.py: a missing
# device, rule, route, or nft handle is not an error — a half-collapsed state
# (e.g. a re-invoked action after a partial failure) converges cleanly.
#
# Inputs:
#   virtual_machine_name  - UUID (for logging / symmetry)
#   virtual_machine_ipv6  - the /128 that was being forwarded
#   role                  - "source" or "target" (which end's state to remove)
#   tunnel_device         - the mig6-<vm8> interface
#   tunnel_port           - the socat carrier's TCP port (to kill the right unit)
#   route_table           - the per-VM return table id (target role only)
#   deassert_proxy_ndp    - "1" (default) removes the /128's proxy-NDP entry on the
#                           uplink; "0" only for a provider that answers NDP upstream
#                           itself (source role only). Mirror of the UNCONDITIONAL
#                           re-assert in migration-source-forward.py — the source
#                           answered NDP for the /128 while forwarding (every provider,
#                           Scaleway EM included — spec/24 §2.0), so collapse must stop
#                           it on every provider. The earlier "Scaleway routed /64 needs
#                           no NDP" assumption was disproven in the field.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run
from atlas._task import TaskInputs, TaskResult
from atlas.network_env import default_route_device


@dataclass(frozen=True)
class ForwardDownInputs(TaskInputs):
	"""Tear down one end of a keep-address migration forward tunnel."""

	command: typing.ClassVar[str] = "migration-forward-down"
	virtual_machine_name: str
	virtual_machine_ipv6: str
	role: str
	tunnel_device: str
	tunnel_port: int
	route_table: int = 0
	deassert_proxy_ndp: str = "1"


@dataclass(frozen=True)
class ForwardDownResult(TaskResult):
	down: bool = True


def main() -> None:
	inputs = ForwardDownInputs.from_args()
	if inputs.role not in ("source", "target"):
		sys.exit(f"role must be 'source' or 'target', got {inputs.role!r}")
	vmv6 = inputs.virtual_machine_ipv6

	if inputs.role == "source":
		_collapse_source(inputs, vmv6)
	else:
		_collapse_target(inputs, vmv6)

	# Common: stop the socat carrier transient unit (kills the tun device with it)
	# and delete the device defensively in case socat already exited but left it
	# behind. Keyed on the same per-port unit name migration-forward-up started.
	unit = f"atlas-mig6-{inputs.tunnel_port}"
	run("sudo systemctl stop {}", unit, check=False)
	run("sudo systemctl reset-failed {}", unit, check=False)
	run("sudo ip link del {}", inputs.tunnel_device, check=False)

	ForwardDownResult().emit()
	print(f"Forward tunnel {inputs.tunnel_device} torn down on the {inputs.role} side.")


def _collapse_source(inputs: ForwardDownInputs, vmv6: str) -> None:
	"""Remove the source-forward state: the /128-into-tunnel route, the two nft
	forward rules, and the proxy-NDP entry (every provider by default — the source
	was answering NDP for the /128 while forwarding; spec/24 §2.0)."""
	run("sudo ip -6 route del {} dev {}", f"{vmv6}/128", inputs.tunnel_device, check=False)

	# Delete every forward-chain rule mentioning this VM's /128, by handle — the
	# same lookup vm-network-down.py does. Covers both the oifname and iifname rules.
	chain = run("sudo nft -a list chain inet atlas forward", check=False)
	for line in chain.splitlines():
		if vmv6 in line and inputs.tunnel_device in line:
			handle = line.split()[-1]
			run("sudo nft delete rule inet atlas forward handle {}", handle, check=False)

	if inputs.deassert_proxy_ndp == "1":
		uplink = default_route_device("-6", tolerate_missing=True)
		if uplink:
			run("sudo ip -6 neigh del proxy {} dev {}", vmv6, uplink, check=False)


def _collapse_target(inputs: ForwardDownInputs, vmv6: str) -> None:
	"""Remove the target return-route state: the `from <vmv6>` rule and the private
	table's default route."""
	table = str(inputs.route_table)
	run("sudo ip -6 rule del from {} lookup {} priority 100", vmv6, table, check=False)
	run("sudo ip -6 route del default dev {} table {}", inputs.tunnel_device, table, check=False)


if __name__ == "__main__":
	main()
