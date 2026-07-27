#!/usr/bin/env python3
# Always-on host daemon: wake a Sleeping VM when it receives an inbound TCP SYN
# (spec/32-sleepy-vms.md). The wake side of the "parked" reachability atlas.park
# installs on sleep — see that module for the network mechanics.
#
# A sleeping VM's /128 is parked: proxy-NDP keeps the host answering for it, an
# off-link route sends inbound packets through `inet atlas forward`, and one rule
# there counts + DROPs a connection-opening TCP SYN into a named counter
# `wake_<uuid>`. This daemon polls those named counters ~once a second; the first
# SYN to a still-sleeping VM makes its counter non-zero, and we do the local wake —
# exactly wake-vm.py's two steps (remove the marker, start the unit). The started
# unit's vm-network-up.py unparks (removing the rule + counter + route) and
# vm-restore.py resumes from the snapshot, so the client's retransmitted SYN
# reaches the live guest.
#
# Why a counter poll and not NFQUEUE/NFLOG: it needs no new host dependency (it
# reuses `nft -j` from the stdlib, like poll-vm-traffic.py). The SYN is dropped and
# the client retransmits, so we only have to DETECT the SYN within ~1s, not deliver
# it. The read is untraced (trace=False) so a per-second poll never floods the
# journal; the rare wake action is traced.
#
# systemd-invoked (atlas-wake-trap.service), NOT a Task: it takes no arguments and
# runs forever. It imports the DURABLE atlas package under /var/lib/atlas/bin
# (placed by bootstrap), like the other systemd hooks.

import json
import os
import sys
import time

# The durable package lives next to this script under /var/lib/atlas/bin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atlas._run import run
from atlas.park import ensure_park_device, park, uuid_for_counter
from atlas.paths import VIRTUAL_MACHINES_DIRECTORY, VirtualMachinePaths

# ~1s: an interactive SSH SYN retransmits after its initial RTO (~1s on Linux), so
# detecting within a second lets the retransmit land on the just-woken guest.
POLL_SECONDS = 1.0


def main() -> None:
	# A sleeping VM's unit is suppressed on reboot (ConditionPathNotExists), so
	# vm-network-up never runs and the park state is gone — rebuild it from the
	# on-disk markers before the poll loop, DB-free, like atlas-pool.service
	# re-asserts the pool.
	_resweep_parked_vms()
	while True:
		try:
			_tick()
		except Exception as error:  # a bad tick must never kill the daemon
			print(f"atlas-wake-trap: tick error: {error}", file=sys.stderr, flush=True)
		time.sleep(POLL_SECONDS)


def _tick() -> None:
	for uuid, packets in _wake_counters().items():
		if packets <= 0:
			continue
		paths = VirtualMachinePaths(uuid)
		if not os.path.exists(paths.sleeping_marker):
			# Already woken (an operator Start, or a previous tick): vm-network-up
			# removes the counter as it rebuilds the real path, so a lingering count
			# here is stale. The marker is the authority that the VM is still asleep.
			continue
		try:
			_wake(paths)
		except Exception as error:  # one VM's failed wake must not skip the others
			print(f"atlas-wake-trap: wake {uuid} failed: {error}", file=sys.stderr, flush=True)


def _wake(paths: VirtualMachinePaths) -> None:
	"""The local wake — exactly wake-vm.py's two steps. Remove the marker FIRST so a
	reboot between the steps leaves nothing suppressing the unit; `systemctl start`
	then runs vm-network-up (which unparks) and vm-restore (fast resume). Both steps
	are idempotent, so a concurrent operator wake() or a second tick is harmless."""
	print(f"atlas-wake-trap: waking {paths.uuid} (inbound TCP SYN)", flush=True)
	run("sudo rm -f {}", paths.sleeping_marker)
	run("sudo systemctl start {}", paths.systemd_unit)


def _wake_counters() -> dict[str, int]:
	"""Map VM uuid -> packet count for every `wake_<hex>` named counter in the atlas
	table. Untraced (trace=False) — this runs every second. Tolerates a missing
	table / malformed JSON (returns {})."""
	output = run("sudo nft -j list counters table inet atlas", check=False, quiet=True, trace=False)
	counters: dict[str, int] = {}
	try:
		data = json.loads(output)
	except (json.JSONDecodeError, ValueError, TypeError):
		return counters
	for item in data.get("nftables", []):
		counter = item.get("counter") if isinstance(item, dict) else None
		if not counter:
			continue
		uuid = uuid_for_counter(str(counter.get("name", "")))
		if uuid is not None:
			counters[uuid] = int(counter.get("packets", 0))
	return counters


def _resweep_parked_vms() -> None:
	"""Re-establish park state for every VM still marked sleeping (and the shared
	atlas-park0 device) at daemon boot. Best-effort per VM: one failure must not
	stop the others or the poll loop."""
	try:
		ensure_park_device()
	except Exception as error:
		print(f"atlas-wake-trap: ensure atlas-park0 failed: {error}", file=sys.stderr, flush=True)
	for uuid in _sleeping_uuids():
		try:
			park(uuid)
		except Exception as error:
			print(f"atlas-wake-trap: re-park {uuid} failed: {error}", file=sys.stderr, flush=True)


def _sleeping_uuids() -> list[str]:
	"""Every VM directory that still carries a `sleeping` marker — the DB-free set
	of parked VMs to re-assert after a reboot."""
	try:
		entries = os.listdir(VIRTUAL_MACHINES_DIRECTORY)
	except OSError:
		return []
	return [uuid for uuid in entries if os.path.exists(VirtualMachinePaths(uuid).sleeping_marker)]


if __name__ == "__main__":
	main()
