"""Pure placement/packing math — strategy scoring, no Frappe.

Shared by the live scorer (`atlas/atlas/placement.py`) and the offline simulator
(`atlas/atlas/packing_sim.py`) so the policy they reason about can't drift.
Everything here is plain numbers, never Server rows: a per-axis budget of `None`
means that axis is uncatalogued → unlimited (the operator vouched for the host by
Activating it).

The three packed axes are `cpu` (`cpu_max_cores` — the guaranteed share, which is
the packing dimension), `memory` (MB) and `disk` (GB). See `spec/28-placement.md`
for why the proportional size ladder makes packing one-dimensional, and why on
*homogeneous* hosts every strategy yields identical utilisation — the strategies
diverge only on heterogeneous fleets and on drainability/defragmentation, which the
simulator quantifies.
"""

from __future__ import annotations

AXES = ("cpu", "memory", "disk")

# Placement strategies. Each names the score that orders the FEASIBLE hosts
# (min-rank wins); they share one feasibility gate and all rank measured hosts
# ahead of unmeasured ones, breaking final ties by creation order.
SPREAD = "Spread"  # worst-fit: emptiest by relative fill — blast radius + burst/resize headroom
BEST_FIT = "Best Fit"  # most-allocated: fullest feasible host — keeps hosts drainable, holes for Dedicated
TETRIS = "Tetris"  # dot-product alignment: match demand shape to host headroom — least stranding
FIRST_FIT = "First Fit"  # creation order — the pre-scorer behaviour; cheapest, most fragmenting
HYBRID = "Hybrid"  # dedicated-class VMs Best Fit (don't fragment the big holes), the rest Spread
STRATEGIES = (SPREAD, BEST_FIT, TETRIS, FIRST_FIT, HYBRID)
DEFAULT_STRATEGY = SPREAD

# A VM is "dedicated-class" when it guarantees a whole core or more (cpu_max_cores ≥
# 1). The Hybrid strategy best-fits these so the scarce large holes stay contiguous,
# while spreading the sub-core shared tiers to keep in-place resize headroom on every
# host. The threshold is on the CPU (guarantee) axis because that is the packing
# dimension and it cleanly separates Dedicated (1.0) from the shared tiers (≤ 0.5).
DEDICATED_CPU_THRESHOLD = 1.0


def _resolve(strategy: str, needs: dict) -> str:
	"""Map the operator's strategy to the one this particular VM is scored under.

	Only `HYBRID` is VM-dependent: a dedicated-class VM (a whole guaranteed core or
	more) is placed Best Fit, everything else Spread. Every other strategy is the same
	for all VMs and passes through unchanged."""
	if strategy == HYBRID:
		return BEST_FIT if needs["cpu"] >= DEDICATED_CPU_THRESHOLD else SPREAD
	return strategy


def _evaluate(
	budgets: dict, used: dict, needs: dict, reserve: float
) -> tuple[bool, int, float, float]:
	"""Feasibility + shape metrics for placing `needs` on one host.

	`budgets`/`used`/`needs` are dicts over `AXES` (a budget of `None` = that axis is
	unmeasured → unlimited). Returns `(fits, unmeasured_count, fill, alignment)`:

	- `fits` — every MEASURED axis has room within its reserved budget
	  `budget × (1 − reserve)`.
	- `fill` — the max post-placement fill across measured axes (the bottleneck),
	  relative to the reserved budget; lower = emptier host afterward.
	- `alignment` — the dot product of normalised demand and normalised free
	  headroom over measured axes: how well the VM's shape matches where the host has
	  room (the Tetris/most-aligned heuristic); higher = better shape match."""
	fits = True
	unmeasured = 0
	fill = 0.0
	alignment = 0.0
	for axis in AXES:
		budget = budgets[axis]
		if budget is None:
			unmeasured += 1
			continue
		allowed = budget * (1.0 - reserve)
		projected = used[axis] + needs[axis]
		if projected > allowed:
			fits = False
		if allowed > 0:
			fill = max(fill, projected / allowed)
		if budget > 0:
			free = max(0.0, budget - used[axis])
			alignment += (needs[axis] / budget) * (free / budget)
	return fits, unmeasured, fill, alignment


def rank_key(
	strategy: str,
	budgets: dict,
	used: dict,
	needs: dict,
	reserve: float,
	tie_breaker,
) -> tuple | None:
	"""The sort key for placing `needs` on one host under `strategy`, or `None` if it
	does not fit.

	Lower wins. The key is `(unmeasured_axis_count, score, tie_breaker)`: measured
	hosts first (a real fill beats an unknown one), then the strategy's score, then
	`tie_breaker` (the host's creation index) for determinism. On a proportional
	catalog with homogeneous hosts every strategy's score ties, so the tie-breaker
	decides and utilisation is identical — the strategies only diverge on
	heterogeneous fleets (spec/28)."""
	fits, unmeasured, fill, alignment = _evaluate(budgets, used, needs, reserve)
	if not fits:
		return None
	strategy = _resolve(strategy, needs)
	if strategy == BEST_FIT:
		score = -fill  # fullest feasible host
	elif strategy == TETRIS:
		score = -alignment  # best shape match
	elif strategy == FIRST_FIT:
		score = 0.0  # pure creation order
	else:
		score = fill  # SPREAD (default): emptiest feasible host
	return (unmeasured, score, tie_breaker)
