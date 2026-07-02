#!/usr/bin/env python3
# Target side of a keep-address migration's cutover (spec/24 §2.3, §2.9.3): the
# return-route policy that forces the guest's replies back UP the tunnel.
#
# The target runs its normal vm-network-up.py at unit start (same netns/veth/tap,
# the same `<vmv6>/128 via fe80::3 dev <host_veth>` route). Inbound therefore
# already flows source -> tunnel -> target veth -> guest once the source-forward
# is installed. The problem is EGRESS: the target sourcing the VM's /128 (which
# belongs to the SOURCE's /64) is dropped at the switch (spec/24 §2.0, verified on
# Scaleway). So the guest's replies must go back up the tunnel and egress at the
# source, which legitimately owns the /64.
#
# This installs a per-VM policy route to do exactly that:
#
#   ip -6 rule add from <vmv6> lookup <table> priority 100
#   ip -6 route replace default dev <tunnel> table <table>
#
# so a packet SOURCED FROM the VM's /128 is steered to a private table whose only
# route sends it out the tunnel. Everything else on the host is unaffected. This
# runs BEFORE migration-source-forward so the return path exists before inbound
# starts arriving.
#
# Idempotent: `ip -6 rule add` is guarded against a duplicate (rules stack, unlike
# `route replace`), and the table's default route is a `replace`.
#
# Inputs:
#   virtual_machine_name  - UUID (for logging / symmetry)
#   virtual_machine_ipv6  - the /128 whose egress is policy-routed
#   tunnel_device         - the mig6-<vm8> interface (already up from forward-up)
#   route_table           - the per-VM table id (controller-derived)

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class TargetReceiveInputs(TaskInputs):
	"""Install the return-route policy that sends the guest's replies up the tunnel."""

	command: typing.ClassVar[str] = "migration-target-receive"
	virtual_machine_name: str
	virtual_machine_ipv6: str
	tunnel_device: str
	route_table: int


@dataclass(frozen=True)
class TargetReceiveResult(TaskResult):
	receiving: bool = True


def main() -> None:
	inputs = TargetReceiveInputs.from_args()
	vmv6 = inputs.virtual_machine_ipv6
	table = str(inputs.route_table)

	if not run_ok("ip link show {}", inputs.tunnel_device):
		sys.exit(f"forward tunnel {inputs.tunnel_device} is not up; run migration-forward-up first")

	# The table's sole route: default out the tunnel. `replace` is idempotent.
	run("sudo ip -6 route replace default dev {} table {}", inputs.tunnel_device, table)

	# The rule that selects the table for packets sourced from this VM. `ip rule
	# add` STACKS (a re-entry would add a duplicate), so guard on the rendered rule
	# already being present in `ip -6 rule show`.
	wanted = f"from {vmv6} lookup {table}"
	existing = run("ip -6 rule show", check=False)
	if wanted not in existing:
		run("sudo ip -6 rule add from {} lookup {} priority 100", vmv6, table)

	TargetReceiveResult().emit()
	print(f"Target return route for {vmv6} -> table {table} -> {inputs.tunnel_device} installed.")


if __name__ == "__main__":
	main()
