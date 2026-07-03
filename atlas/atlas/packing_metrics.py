"""Pure fleet-packing metrics — no Frappe, no simulator state.

Computed from a snapshot of per-host budgets + usage (the same per-axis numbers
`capacity_for_server` produces, and what `packing_sim` tracks). These are the
numbers `spec/24-placement.md` says to watch: committed utilisation, the
cell-compaction ratio, stranded capacity / axis imbalance, and sellable share-unit
slots. The simulator uses them to score a run; they also work standalone against any
list of objects exposing `.budget` (dict over `AXES`), `.used` (dict) and
`.vm_count` — e.g. a thin adapter over `capacity_for_server`.
"""

from __future__ import annotations

import math

from atlas.atlas.packing import AXES
from atlas.atlas.sizes import SHARE_UNIT

# The share unit's cost keyed by the packing-axis names (SHARE_UNIT is keyed by the
# VM resource fields). One source of truth: sizes.SHARE_UNIT (a Shared 1x).
UNIT_COST = {
	"cpu": SHARE_UNIT["cpu_max_cores"],
	"memory": SHARE_UNIT["memory_megabytes"],
	"disk": SHARE_UNIT["disk_gigabytes"],
}


def committed_utilization(hosts) -> dict:
	"""Per-axis committed/usable across the fleet: `sum(used) / sum(budget)`.

	The oversubscription factor can push the CPU axis above 1.0; memory and disk are
	hard fits and stay ≤ 1.0. A dict over `AXES`."""
	out = {}
	for axis in AXES:
		budget = sum(h.budget[axis] for h in hosts)
		used = sum(h.used[axis] for h in hosts)
		out[axis] = (used / budget) if budget else 0.0
	return out


def share_units(host) -> dict:
	"""Sellable Shared-1x slots on one host: `{total, used, free}` (see spec/24)."""
	total = min(int(host.budget[axis] // UNIT_COST[axis]) for axis in AXES)
	used = max((math.ceil(host.used[axis] / UNIT_COST[axis]) for axis in AXES), default=0)
	return {"total": total, "used": used, "free": total - used}


def stranded(host) -> dict:
	"""Per-axis capacity the bottleneck axis makes unsellable at full subscription:
	`budget − share_units.total × unit_cost`."""
	total = share_units(host)["total"]
	return {axis: host.budget[axis] - total * UNIT_COST[axis] for axis in AXES}


def imbalance(host) -> float:
	"""`max_axis(fill) − min_axis(fill)` — how lopsided a host's usage is. High
	imbalance means one axis is nearly full while another sits idle (stranding)."""
	fills = [(host.used[axis] / host.budget[axis]) if host.budget[axis] else 0.0 for axis in AXES]
	return max(fills) - min(fills)


def compaction_ratio(hosts) -> float:
	"""`lower_bound_host_count / hosts_in_use` (spec/24 §6).

	The lower bound is the fewest hosts (largest-first) whose cumulative budget covers
	the committed totals, taken as the max over axes — the theoretical floor a perfect
	repack could reach. `~1.0` = already tight; `< 1.0` = consolidation is possible
	(that many hosts could be drained). Returns 1.0 for an empty fleet."""
	in_use = sum(1 for h in hosts if h.vm_count)
	if not in_use:
		return 1.0
	committed = {axis: sum(h.used[axis] for h in hosts) for axis in AXES}
	lower_bound = 0
	for axis in AXES:
		budgets = sorted((h.budget[axis] for h in hosts), reverse=True)
		accumulated = 0.0
		count = 0
		for budget in budgets:
			if accumulated >= committed[axis]:
				break
			accumulated += budget
			count += 1
		lower_bound = max(lower_bound, count)
	return lower_bound / in_use
