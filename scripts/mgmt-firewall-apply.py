#!/usr/bin/env python3
# Lock down this Atlas host's PUBLIC interface: load the inet atlas_mgmt nftables
# table (default-deny inbound except the WireGuard UDP port + public_allow_ports +
# loopback/established/ICMP; wg0 and every non-public iface stay open) and ARM an
# auto-revert. The lockdown is live immediately but undoes itself after
# --revert-seconds unless firewall-confirm cancels it first — so a failed handoff
# can never permanently lock Central or the operator out (spec/21-tunnel.md).
#
# Runs on the Atlas host via atlas.atlas.local_task.run_local_task; nft / systemd-run
# / systemctl are sudoers-pinned. The public interface is discovered from the default
# route when --public-interface is omitted.

import os
import sys
import typing
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import atlas.mgmt_firewall as firewall
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class FirewallApplyInputs(TaskInputs):
	"""Apply the management-plane firewall with an armed auto-revert."""

	command: typing.ClassVar[str] = "mgmt-firewall-apply"
	wg_port: int = 51820  # the one public inbound port (the wg handshake)
	public_interface: str = ""  # default: discover from the default route
	revert_seconds: int = 180  # auto-revert window unless confirmed
	public_allow_ports: list[str] = field(default_factory=list)  # extra public TCP ports (default none)


@dataclass(frozen=True)
class FirewallApplyResult(TaskResult):
	public_interface: str
	wg_port: int
	revert_seconds: int
	public_allow_ports: list[str]


def main() -> None:
	inputs = FirewallApplyInputs.from_args()
	public_interface = inputs.public_interface or firewall.discover_public_interface()

	firewall.apply(public_interface, inputs.wg_port, inputs.public_allow_ports, inputs.revert_seconds)

	FirewallApplyResult(
		public_interface=public_interface,
		wg_port=inputs.wg_port,
		revert_seconds=inputs.revert_seconds,
		public_allow_ports=inputs.public_allow_ports,
	).emit()
	print(
		f"Locked {public_interface}: only udp/{inputs.wg_port} (+{inputs.public_allow_ports or 'no'} "
		f"extra ports) public; auto-revert armed for {inputs.revert_seconds}s."
	)


if __name__ == "__main__":
	main()
