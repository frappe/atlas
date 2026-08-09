"""Discriminating strategy sweep for PR #30 packing.

Heterogeneous fleet (small/medium/large droplets) with resizes (upgrade hazard)
and drops (finite lifetimes) ON — the regime where strategies actually diverge.
Sweeps offered load to find the band where placement decisions matter, and
averages every point over many seeds so an edge is real, not one lucky workload.
"""

from __future__ import annotations

import os
import sys

# repo root (…/llm/eval/<this>.py → three levels up), so `atlas.atlas.*` imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.atlas import packing
from atlas.atlas.packing_sim import SimConfig, compare_seeds

# A heterogeneous fleet: small(4c/8G/160), medium(8c/16G/320), large(16c/32G/640)
# effective budgets. Cycled across host_count → equal counts of each. RAM binds.
SMALL = (4.0, 8192.0, 160.0)
MEDIUM = (8.0, 16384.0, 320.0)
LARGE = (16.0, 32768.0, 640.0)
FLEET = (SMALL, MEDIUM, LARGE)
HOST_COUNT = 12  # 4 of each shape → 4*(16+32+64) = 448 share units of RAM-bound capacity

SEEDS = 24
MEAN_LIFETIME = 500.0
UPGRADE_HAZARD = 0.002  # ~1 resize-up attempt per VM lifetime → exercises resize path
DURATION = 20000.0


def offered_units(arrival_rate: float) -> float:
	# mean VM size under DEFAULT_PLAN_MIX weights (4,3,2,1,1) over units (1,2,4,8,16)
	mean_units = (4 * 1 + 3 * 2 + 2 * 4 + 1 * 8 + 1 * 16) / (4 + 3 + 2 + 1 + 1)
	return arrival_rate * MEAN_LIFETIME * mean_units


def run_point(arrival_rate: float, reserve: float = 0.0) -> dict:
	cfg = SimConfig(
		seed=1000,
		duration=DURATION,
		arrival_rate=arrival_rate,
		host_count=HOST_COUNT,
		host_shapes=FLEET,
		mean_lifetime=MEAN_LIFETIME,
		upgrade_hazard=UPGRADE_HAZARD,
		reserve=reserve,
	)
	return compare_seeds(cfg, SEEDS)


def fmt_row(name, m):
	return (
		f"{name:<10} accept {m['acceptance'][0]:.3f}±{m['acceptance'][1]:.3f}  "
		f"blk-lrg {m['blocked_largest_rate'][0]:.3f}  "
		f"in-place {m['in_place_upgrade_rate'][0]:.3f}  "
		f"migr {m['forced_migrations'][0]:5.1f}±{m['forced_migrations'][1]:4.1f}  "
		f"hosts {m['mean_hosts_in_use'][0]:4.1f}  "
		f"compact {m['mean_compaction'][0]:.3f}"
	)


if __name__ == "__main__":
	# 448 units capacity. Sweep arrival rate to walk offered load ~50%→130%.
	for rate in (0.10, 0.13, 0.16, 0.19, 0.23):
		load = offered_units(rate) / 448.0
		print(f"\n=== arrival_rate={rate}  offered≈{load * 100:.0f}% of capacity  reserve=0% ===")
		agg = run_point(rate, reserve=0.0)
		for strat in packing.STRATEGIES:
			print(fmt_row(strat, agg[strat]))
