"""Low-load arbitrary-shape check: does any strategy win ACCEPTANCE where the binding
constraint is cross-axis fragmentation (not saturation)? This is the only regime where
a shape-aware scorer (Tetris/Best Fit) could plausibly beat Spread on feasibility."""

import os
import sys

# this script's own dir (for `arbitrary_sim`) + repo root (for `atlas.atlas.*`)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from arbitrary_sim import compare_seeds

from atlas.atlas import packing

for rate in (0.06, 0.09, 0.12, 0.15):
	base = {
		"seed": 500,
		"duration": 20000.0,
		"arrival_rate": rate,
		"host_count": 12,
		"mean_lifetime": 500.0,
		"upgrade_hazard": 0.002,
		"reserve": 0.0,
	}
	agg = compare_seeds(base, 24)
	print(f"\n=== arrival_rate={rate}  reserve=0%  (arbitrary, 24 seeds) ===")
	print(f"{'strategy':<10} {'accept':>14} {'blk-lopsided':>13} {'migr':>10} {'imbal':>7}")
	for s in packing.STRATEGIES:
		m = agg[s]
		print(
			f"{s:<10} {m['acceptance'][0]:.3f}±{m['acceptance'][1]:.3f}   "
			f"{m['blocked_lopsided_rate'][0]:>11.3f} "
			f"{m['forced_migrations'][0]:>6.0f}±{m['forced_migrations'][1]:<3.0f} "
			f"{m['mean_imbalance'][0]:>7.3f}"
		)
