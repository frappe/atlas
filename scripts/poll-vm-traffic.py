#!/usr/bin/env python3
# Poll per-VM nftables byte counters for a set of VMs on this host.
# Returns active:bool per VM (True = bytes changed since last poll, False = idle).
#
# Delta computation runs entirely on the host: the last-seen byte total is stored
# in /var/lib/atlas/virtual-machines/<uuid>/traffic-counter.json so the controller
# only ever sees a bool — raw counters are host-local, ephemeral, and not DB state.
#
# Counter anomalies (reset, missing rule) are treated as active=True so a VM is
# never incorrectly put to sleep after a chain flush or host reboot.

import json
import os
import re
import sys
import typing
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class PollVmTrafficInputs(TaskInputs):
	"""Poll nftables byte counters for the given VMs; return active:bool per VM."""

	command: typing.ClassVar[str] = "poll-vm-traffic"
	vms_json: str  # JSON: [{"name": "<uuid>", "ipv6_address": "<fdaa::...>"}]


@dataclass(frozen=True)
class PollVmTrafficResult(TaskResult):
	counters: dict = field(default_factory=dict)  # {"<vm-name>": {"active": bool}}


def main() -> None:
	inputs = PollVmTrafficInputs.from_args()
	vms = json.loads(inputs.vms_json)

	if not vms:
		PollVmTrafficResult(counters={}).emit()
		return

	if not run_ok("sudo nft list chain inet atlas forward"):
		# Chain not yet created (host just rebooted before any VM started).
		# Treat all as active so none are slept on a post-reboot poll anomaly.
		result = {vm["name"]: {"active": True} for vm in vms}
		PollVmTrafficResult(counters=result).emit()
		return

	chain_dump = run("sudo nft list chain inet atlas forward")
	result = {}
	for vm in vms:
		name = vm["name"]
		ipv6 = vm["ipv6_address"]
		result[name] = {"active": _is_active(name, ipv6, chain_dump)}

	PollVmTrafficResult(counters=result).emit()


def _is_active(vm_name: str, ipv6: str, chain_dump: str) -> bool:
	"""Compare the VM's current nft byte total to the last-seen value.
	Returns True if traffic changed (active) or the counter is anomalous."""
	current = _read_bytes(ipv6, chain_dump)
	counter_file = VirtualMachinePaths(vm_name).traffic_counter_file
	last = _load_last(counter_file)
	_save_last(counter_file, current)

	if last is None:
		# First poll — no baseline yet; don't sleep a VM we've never observed.
		return True
	if current < last:
		# Counter reset (chain flush or host reboot) — treat as active.
		return True
	return current > last


def _read_bytes(ipv6: str, chain_dump: str) -> int:
	"""Sum bytes from all nft forward rules that mention this VM's IPv6 address.
	Summing handles the uncommon case where duplicate rules exist (two starts
	without an intervening chain flush)."""
	total = 0
	for line in chain_dump.splitlines():
		if ipv6 in line and "counter" in line:
			m = re.search(r"\bbytes (\d+)", line)
			if m:
				total += int(m.group(1))
	return total


def _load_last(counter_file: str) -> int | None:
	try:
		# nosemgrep: frappe-security-file-traversal -- host script, not a Frappe request
		# path: counter_file is VirtualMachinePaths(<uuid>).traffic_counter_file, built
		# from a controller-supplied UUID, never from user input.
		with open(counter_file) as f:
			return int(json.load(f)["bytes"])
	except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError, TypeError):
		return None


def _save_last(counter_file: str, current: int) -> None:
	try:
		# nosemgrep: frappe-security-file-traversal -- see _load_last above.
		with open(counter_file, "w") as f:
			json.dump({"bytes": current}, f)
	except OSError:
		pass


if __name__ == "__main__":
	main()
