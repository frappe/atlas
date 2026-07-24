#!/usr/bin/env python3
# Source side of a migration cutover (spec/31 §16.3 — soft sequencing): withdraw
# the VM's private /128 from THIS (source) host's local-ownership cache, so the
# source's atlas-networkd stops advertising it BEFORE the target host's
# provision-vm boots the guest and starts advertising the SAME /128. Two origins
# advertising one /128 is the §7.3 conflict — ANCP drops it from every host's
# wg-mesh AllowedIPs, blackholing the migrated VM's private plane for the whole
# (multi-minute) hydration window. Withdrawing here, first, keeps the two
# advertisements non-overlapping (the §16.3 withdraw-from-source-THEN-advertise
# ordering the migration controller owns).
#
# The private /128 is HOST-INDEPENDENT (a pure HKDF of tenant+VM — it survives the
# move byte-for-byte), so the cache entry is the same string on both hosts; only
# WHICH host advertises it must change, one at a time.
#
# By the time this runs the source VM is already Stopped (spec/24 §0.3: it stays
# Stopped from Pending until Cleanup) and its ExecStopPost (vm-network-down.py)
# already removed this /128 at the Pending stop — so this is normally a re-assert.
# It is kept as an EXPLICIT, idempotent controller step (not a bare side effect of
# the unit stop) so the ordering is guaranteed at the cutover seam even if the
# Pending withdrawal did not land (a resumed/re-entered migration, a cache the
# stop left populated). It touches ONLY the ownership cache — no netns, veth, disk
# or LV — so it never disturbs the source copy the rollback-through-Hydrating path
# (spec/24 §0.3) depends on.
#
# Idempotent: remove_local_owned is a no-op if the /128 is already gone (or the
# cache is absent). No-op input (empty private_address) is a clean no-op too, so a
# tenant-less VM's cutover can call it unconditionally.
#
# Inputs:
#   virtual_machine_name  - UUID (for logging / symmetry)
#   private_address       - the fdaa:: /128 to withdraw (empty = tenant-less, no-op)

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs, TaskResult
from atlas.networkd.localownership import remove_local_owned


@dataclass(frozen=True)
class WithdrawPrivateSourceInputs(TaskInputs):
	"""Withdraw a migrating VM's private /128 from the source host's ownership cache."""

	command: typing.ClassVar[str] = "migration-withdraw-private-source"
	virtual_machine_name: str
	private_address: str = ""


@dataclass(frozen=True)
class WithdrawPrivateSourceResult(TaskResult):
	withdrawn: bool = True


def main() -> None:
	inputs = WithdrawPrivateSourceInputs.from_args()
	if inputs.private_address:
		remove_local_owned(inputs.private_address)
		print(f"Withdrew private {inputs.private_address} from the source ownership cache.")
	else:
		print(f"{inputs.virtual_machine_name} has no private /128; nothing to withdraw.")
	WithdrawPrivateSourceResult().emit()


if __name__ == "__main__":
	main()
