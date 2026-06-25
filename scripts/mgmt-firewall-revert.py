#!/usr/bin/env python3
# Restore open public access: cancel the armed auto-revert, delete the inet
# atlas_mgmt table, and remove the persisted ruleset + disable the boot unit so a
# reboot does not re-lock. This is both the rollback path and what the armed timer's
# effect mirrors (spec/21-tunnel.md). Runs on the Atlas host via
# atlas.atlas.local_task.run_local_task; nft / systemctl / rm are sudoers-pinned.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import atlas.mgmt_firewall as firewall
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class FirewallRevertInputs(TaskInputs):
	"""Revert the management-plane firewall (restore open public access)."""

	command: typing.ClassVar[str] = "mgmt-firewall-revert"


@dataclass(frozen=True)
class FirewallRevertResult(TaskResult):
	reverted: bool


def main() -> None:
	FirewallRevertInputs.from_args()

	firewall.revert()

	FirewallRevertResult(reverted=True).emit()
	print("Management-plane firewall reverted; public access restored.")


if __name__ == "__main__":
	main()
