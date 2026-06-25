#!/usr/bin/env python3
# Confirm the lockdown: cancel the armed auto-revert and make the locked ruleset the
# boot default (write the persisted include + enable the fail-closed boot unit
# atlas-mgmt-firewall.service, ordered Before=network-pre.target). Called by Central
# OVER THE TUNNEL (central_link.confirm_tunnel) — arriving over wg0 proves end-to-end
# reachability before the public side is made permanently dark (spec/21-tunnel.md).
#
# Runs on the Atlas host via atlas.atlas.local_task.run_local_task; nft / systemctl /
# install are sudoers-pinned. Pass the SAME interface/port/allow-ports apply used so
# the persisted ruleset matches the live one.

import os
import sys
import typing
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import atlas.mgmt_firewall as firewall
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class FirewallConfirmInputs(TaskInputs):
	"""Persist the management-plane firewall and cancel the auto-revert."""

	command: typing.ClassVar[str] = "mgmt-firewall-confirm"
	wg_port: int = 51820
	public_interface: str = ""  # default: discover from the default route
	public_allow_ports: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FirewallConfirmResult(TaskResult):
	confirmed: bool
	public_interface: str


def main() -> None:
	inputs = FirewallConfirmInputs.from_args()
	public_interface = inputs.public_interface or firewall.discover_public_interface()

	firewall.persist(public_interface, inputs.wg_port, inputs.public_allow_ports)

	FirewallConfirmResult(confirmed=True, public_interface=public_interface).emit()
	print(f"Firewall confirmed on {public_interface}; auto-revert cancelled, lockdown persisted.")


if __name__ == "__main__":
	main()
