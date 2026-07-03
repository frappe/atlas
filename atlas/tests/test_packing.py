"""The pure packing layer: strategy scoring, fleet metrics, and the simulator.

No Frappe, no host — `packing`, `packing_metrics`, and `packing_sim` are plain
Python (the only Atlas import underneath is `sizes`, itself pure). Runs under the
Frappe test runner like everything else, but needs nothing from it. These pin the
strategy semantics (spec/24), the metric math, and that the simulator is
deterministic and behaves sanely across arrivals, drops, resizes, and the edges.
"""

import unittest

from atlas.atlas import packing, packing_metrics, packing_sim
from atlas.atlas.packing import BEST_FIT, FIRST_FIT, HYBRID, SPREAD, TETRIS
from atlas.atlas.packing_sim import SimConfig

_FULL = {"cpu": 8.0, "memory": 16384.0, "disk": 320.0}
_EMPTY = {"cpu": 0.0, "memory": 0.0, "disk": 0.0}
_SHARED_1X = {"cpu": 0.0625, "memory": 512.0, "disk": 10.0}


def _used(cpu=0.0, memory=0.0, disk=0.0) -> dict:
	return {"cpu": cpu, "memory": memory, "disk": disk}


class _StubHost:
	"""Minimal duck-type for the metrics functions."""

	def __init__(self, budget, used, vm_count=0):
		self.budget = budget
		self.used = used
		self.vm_count = vm_count


class TestStrategyScoring(unittest.TestCase):
	def test_infeasible_returns_none(self) -> None:
		# A 512 MB VM can't fit where only 100 MB is free.
		budgets = {"cpu": 8.0, "memory": 512.0, "disk": 320.0}
		self.assertIsNone(
			packing.rank_key(SPREAD, budgets, _used(memory=400.0), _SHARED_1X, 0.0, 0)
		)

	def test_unmeasured_axis_is_unlimited_and_counted(self) -> None:
		# A None budget fits anything, but bumps the unmeasured count (first rank
		# element) so a measured host always outranks it.
		budgets = {"cpu": None, "memory": 16384.0, "disk": 320.0}
		key = packing.rank_key(SPREAD, budgets, _EMPTY, _SHARED_1X, 0.0, 0)
		self.assertIsNotNone(key)
		self.assertEqual(key[0], 1, "one unmeasured axis")
		measured = packing.rank_key(SPREAD, _FULL, _EMPTY, _SHARED_1X, 0.0, 0)
		self.assertLess(measured[0], key[0], "measured host ranks ahead")

	def test_spread_prefers_the_emptier_host(self) -> None:
		empty = packing.rank_key(SPREAD, _FULL, _EMPTY, _SHARED_1X, 0.0, 0)
		half = packing.rank_key(SPREAD, _FULL, _used(4.0, 8192.0, 160.0), _SHARED_1X, 0.0, 1)
		self.assertLess(empty, half, "spread picks the emptier host")

	def test_best_fit_prefers_the_fuller_host(self) -> None:
		empty = packing.rank_key(BEST_FIT, _FULL, _EMPTY, _SHARED_1X, 0.0, 0)
		half = packing.rank_key(BEST_FIT, _FULL, _used(4.0, 8192.0, 160.0), _SHARED_1X, 0.0, 1)
		self.assertLess(half, empty, "best fit packs the fuller host first")

	def test_tetris_prefers_the_shape_matched_host(self) -> None:
		# Both hosts fit the VM, but B's free capacity is aligned with the demand
		# (lots of free memory/disk, the axes the proportional VM leans on).
		a = packing.rank_key(TETRIS, _FULL, _used(memory=15000.0), _SHARED_1X, 0.0, 0)
		b = packing.rank_key(TETRIS, _FULL, _used(cpu=7.5), _SHARED_1X, 0.0, 1)
		self.assertLess(b, a, "tetris picks the host whose free shape matches demand")

	def test_first_fit_is_pure_creation_order(self) -> None:
		# First fit ignores fill: among feasible hosts the lowest tie-breaker wins.
		older = packing.rank_key(FIRST_FIT, _FULL, _used(4.0, 8192.0, 160.0), _SHARED_1X, 0.0, 0)
		newer = packing.rank_key(FIRST_FIT, _FULL, _EMPTY, _SHARED_1X, 0.0, 1)
		self.assertLess(older, newer, "first fit takes the earliest feasible host")

	def test_hybrid_best_fits_dedicated_and_spreads_the_rest(self) -> None:
		# A dedicated-class VM (a full guaranteed core) is scored Best Fit; a shared
		# tier is scored Spread. Same two hosts (one empty, one half), so the two base
		# strategies pick opposite hosts and Hybrid must match each one.
		dedicated = {"cpu": 1.0, "memory": 8192.0, "disk": 160.0}
		half = _used(4.0, 8192.0, 160.0)
		# Dedicated → Best Fit → prefers the fuller host.
		self.assertLess(
			packing.rank_key(HYBRID, _FULL, half, dedicated, 0.0, 1),
			packing.rank_key(HYBRID, _FULL, _EMPTY, dedicated, 0.0, 0),
		)
		# Shared → Spread → prefers the emptier host.
		self.assertLess(
			packing.rank_key(HYBRID, _FULL, _EMPTY, _SHARED_1X, 0.0, 0),
			packing.rank_key(HYBRID, _FULL, half, _SHARED_1X, 0.0, 1),
		)

	def test_reserve_shrinks_the_feasible_budget(self) -> None:
		budgets = {"cpu": 8.0, "memory": 1024.0, "disk": 320.0}
		need = {"cpu": 0.0625, "memory": 768.0, "disk": 10.0}
		self.assertIsNotNone(packing.rank_key(SPREAD, budgets, _EMPTY, need, 0.0, 0), "raw budget fits")
		self.assertIsNone(
			packing.rank_key(SPREAD, budgets, _EMPTY, need, 0.5, 0), "a 50% reserve blocks it"
		)


class TestMetrics(unittest.TestCase):
	def test_committed_utilization(self) -> None:
		hosts = [_StubHost(_FULL, _used(4.0, 8192.0, 160.0), vm_count=1)]
		util = packing_metrics.committed_utilization(hosts)
		self.assertAlmostEqual(util["cpu"], 0.5)
		self.assertAlmostEqual(util["memory"], 0.5)

	def test_share_units_and_stranded(self) -> None:
		# 8c / 16 GB / 320 GB: RAM binds at 16384/512 = 32 units; CPU and disk strand.
		host = _StubHost(_FULL, _EMPTY)
		self.assertEqual(packing_metrics.share_units(host)["total"], 32)
		stranded = packing_metrics.stranded(host)
		self.assertAlmostEqual(stranded["cpu"], 8.0 - 32 * 0.0625)  # 6.0 cores
		self.assertEqual(stranded["memory"], 0)

	def test_compaction_ratio_flags_consolidation(self) -> None:
		# Two half-full hosts hold what one host could — compaction < 1 (drainable).
		hosts = [
			_StubHost(_FULL, _used(2.0, 4096.0, 80.0), vm_count=1),
			_StubHost(_FULL, _used(2.0, 4096.0, 80.0), vm_count=1),
		]
		self.assertLess(packing_metrics.compaction_ratio(hosts), 1.0)
		# One host carrying its own load is already tight.
		tight = [_StubHost(_FULL, _used(7.0, 14000.0, 300.0), vm_count=1)]
		self.assertEqual(packing_metrics.compaction_ratio(tight), 1.0)


class TestSimulator(unittest.TestCase):
	def _config(self, **overrides) -> SimConfig:
		base = {"seed": 7, "duration": 3000.0, "arrival_rate": 1.0, "host_count": 15}
		base.update(overrides)
		return SimConfig(**base)

	def test_deterministic(self) -> None:
		config = self._config()
		self.assertEqual(packing_sim.run(config), packing_sim.run(config))

	def test_same_workload_across_strategies(self) -> None:
		# compare() replays ONE workload, so every strategy sees the same arrivals →
		# identical total attempts. Only the placement outcomes differ.
		results = packing_sim.compare(self._config())
		attempted = {stats["attempted"] for stats in results.values()}
		self.assertEqual(len(attempted), 1, "every strategy sees the same workload")
		self.assertGreater(attempted.pop(), 0)

	def test_drops_free_capacity(self) -> None:
		# Under saturating load, short-lived VMs (fast drops) accept far more than
		# long-lived ones — departures return capacity to the pool.
		saturating = {"host_count": 5, "arrival_rate": 2.0, "duration": 4000.0}
		churny = packing_sim.run(self._config(mean_lifetime=30.0, **saturating))
		sticky = packing_sim.run(self._config(mean_lifetime=5000.0, **saturating))
		self.assertGreater(churny["acceptance"], sticky["acceptance"])

	def test_resizes_in_place_and_forced_migration(self) -> None:
		# Small hosts packed with small VMs; upgrades can't always grow in place, so
		# some force a migration. The three outcomes partition every upgrade attempt.
		config = self._config(
			host_count=8,
			arrival_rate=1.5,
			mean_lifetime=400.0,
			upgrade_hazard=0.02,
			host_shapes=((1.0, 2048.0, 40.0),),  # a Shared-8x-sized host: tight for growth
		)
		stats = packing_sim.run(config)
		self.assertGreater(
			stats["in_place_upgrade_rate"], 0.0, "some upgrades grow in place on an idle-ish host"
		)
		self.assertGreater(stats["forced_migrations"], 0, "a full host forces a migration")

	def test_best_fit_uses_no_more_hosts_than_spread(self) -> None:
		# At moderate load best fit concentrates VMs → it lights up fewer hosts than
		# spread, which fans them out. (Same workload via compare().)
		results = packing_sim.compare(self._config(host_count=30, mean_lifetime=100.0))
		self.assertLessEqual(
			results[BEST_FIT]["mean_hosts_in_use"],
			results[SPREAD]["mean_hosts_in_use"] + 1e-9,
		)

	def test_blocked_largest_is_the_fragmentation_canary(self) -> None:
		# When the fleet is pressured, the largest (Dedicated) plan is blocked at a
		# higher rate than the fleet average — scattered free units can't seat it.
		stats = packing_sim.run(
			self._config(strategy=SPREAD, host_count=4, arrival_rate=2.0, mean_lifetime=200.0)
		)
		self.assertGreater(stats["blocked"], 0, "the fleet is pressured")
		overall_block_rate = stats["blocked"] / stats["attempted"]
		self.assertGreater(
			stats["blocked_largest_rate"],
			overall_block_rate,
			"the largest plan is blocked more than the fleet average",
		)

	def test_hybrid_tracks_spread_not_best_fit_on_migrations(self) -> None:
		# Hybrid spreads every upgradeable (shared) VM, so its forced-migration count
		# tracks Spread's and is a fraction of Best Fit's — it is a low-migration
		# middle ground, NOT a migration reducer. Its statistically-significant win is
		# Dedicated acceptance (blocked-largest); that is a many-seed effect measured
		# with compare_seeds and documented in spec/24, not a single-seed unit assert.
		# Moderate load (not saturated) — that's where Best Fit's migration blow-up
		# is largest and the middle-ground property is clearest.
		results = packing_sim.compare(
			self._config(host_count=24, arrival_rate=1.0, mean_lifetime=90.0, upgrade_hazard=0.02)
		)
		self.assertLess(
			results[HYBRID]["forced_migrations"],
			0.5 * results[BEST_FIT]["forced_migrations"],
			"hybrid stays far below best fit's migration blow-up",
		)

	def test_saturation_and_slack_edges(self) -> None:
		# Ample fleet → everyone is admitted; a starved fleet blocks.
		roomy = packing_sim.run(self._config(host_count=200, arrival_rate=0.5, mean_lifetime=50.0))
		self.assertAlmostEqual(roomy["acceptance"], 1.0)
		starved = packing_sim.run(self._config(host_count=1, arrival_rate=3.0, mean_lifetime=5000.0))
		self.assertLess(starved["acceptance"], 1.0)

	def test_no_hosts_blocks_everything(self) -> None:
		stats = packing_sim.run(self._config(host_count=0))
		self.assertEqual(stats["acceptance"], 0.0)
		self.assertEqual(stats["mean_hosts_in_use"], 0.0)


if __name__ == "__main__":
	unittest.main()
