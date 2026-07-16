"""Offline packing simulator — compare placement strategies before changing production.

Event-driven: Poisson arrivals by plan mix, per-plan exponential lifetimes (the VM
*drops* those departures model), and an exponential upgrade hazard (the *resizes*).
Each arrival is placed with the SAME scorer production uses
(`atlas.atlas.packing.rank_key`), so a strategy that wins here is the one to set on
`Atlas Settings.placement_strategy`.

It simulates the two events not yet wired into the app — VM drops and in-place
resizes / forced migrations — because they are cheap to model and are exactly what
distinguishes the strategies (on the proportional ladder with uniform hosts every
strategy ties; they diverge on heterogeneous fleets and on drainability, spec/28).
The workload is generated ONCE and replayed under each strategy, so the comparison
is fair (same arrivals, lifetimes, upgrades) and reproducible from the seed.

Reported per strategy: acceptance, blocked-largest-plan rate (the fragmentation
canary), in-place upgrade success rate, FORCED MIGRATIONS, mean committed
utilisation, mean hosts in use, and the compaction ratio.

Run:  python3 -m atlas.atlas.packing_sim --help
"""

from __future__ import annotations

import argparse
import heapq
import random
import statistics
from dataclasses import dataclass, field

from atlas.atlas import packing, packing_metrics
from atlas.atlas.sizes import SIZE_PRESETS

_PLANS = list(SIZE_PRESETS)
_LARGEST_PLAN = _PLANS[-1]

# A realistic default cloud host shape as an EFFECTIVE per-axis budget:
# (cpu_max_cores, memory_mb, disk_gb). 8 cores / 16 GB / 320 GB — RAM binds first
# (2 GB per core), the shape spec/28 calls out as the common cloud case.
DEFAULT_HOST_SHAPE = (8.0, 16384.0, 320.0)

# Skewed toward the small shared tiers, one long tail of Dedicated — the churny mix
# the fragmentation canary (blocked-largest) is most sensitive to.
DEFAULT_PLAN_MIX = {
	"Shared 1x": 4.0,
	"Shared 2x": 3.0,
	"Shared 4x": 2.0,
	"Shared 8x": 1.0,
	"Dedicated 1x": 1.0,
}


def plan_demand(plan: str) -> dict:
	"""The per-axis demand vector for a size preset (the packing-axis view of it)."""
	preset = SIZE_PRESETS[plan]
	return {
		"cpu": float(preset["cpu_max_cores"]),
		"memory": float(preset["memory_megabytes"]),
		"disk": float(preset["disk_gigabytes"]),
	}


def _next_plan(plan: str) -> str | None:
	"""The next tier up the ladder, or None if already the largest."""
	index = _PLANS.index(plan)
	return _PLANS[index + 1] if index + 1 < len(_PLANS) else None


@dataclass
class SimConfig:
	"""Every knob the operator can turn to reach their own conclusions."""

	strategy: str = packing.SPREAD
	seed: int = 0
	duration: float = 10000.0  # sim-time units
	arrival_rate: float = 1.0  # arrivals per unit time (Poisson λ)
	host_count: int = 20
	host_shapes: tuple = (DEFAULT_HOST_SHAPE,)  # cycled across the fleet (mixed = heterogeneous)
	plan_mix: dict = field(default_factory=lambda: dict(DEFAULT_PLAN_MIX))
	mean_lifetime: float = 500.0  # mean VM lifetime (exponential)
	upgrade_hazard: float = 0.0  # per-unit-time rate a VM asks to grow one tier (0 = never)
	reserve: float = 0.0  # arrival headroom fraction placement leaves free (resize spends it)


@dataclass
class _Request:
	"""A pre-generated arrival, independent of any strategy so the workload replays
	identically. `upgrade_offsets` are times after arrival at which it attempts to
	grow one tier (only fired while the VM is alive)."""

	arrival: float
	plan: str
	lifetime: float
	upgrade_offsets: list


class _Host:
	def __init__(self, host_id: int, shape) -> None:
		self.host_id = host_id
		self.budget = {"cpu": shape[0], "memory": shape[1], "disk": shape[2]}
		self.used = {axis: 0.0 for axis in packing.AXES}
		self.vm_count = 0

	def add(self, demand: dict) -> None:
		for axis in packing.AXES:
			self.used[axis] += demand[axis]
		self.vm_count += 1

	def remove(self, demand: dict) -> None:
		for axis in packing.AXES:
			self.used[axis] -= demand[axis]
		self.vm_count -= 1

	def in_place_fits(self, delta: dict) -> bool:
		# A resize spends the reserve, so it checks the FULL budget.
		return all(self.used[axis] + delta[axis] <= self.budget[axis] for axis in packing.AXES)


class _VM:
	__slots__ = ("alive", "demand", "host", "plan")

	def __init__(self, plan: str, demand: dict, host: "_Host") -> None:
		self.plan = plan
		self.demand = demand
		self.host = host
		self.alive = True


def generate_workload(config: SimConfig) -> list:
	"""Sample the arrival stream once (seeded) so every strategy replays it.

	Poisson arrivals (exponential gaps at rate `arrival_rate`), plan by `plan_mix`,
	exponential lifetime, and a thinned exponential process of upgrade attempts at
	`upgrade_hazard` over the VM's life."""
	rng = random.Random(config.seed)
	plans = list(config.plan_mix)
	weights = [config.plan_mix[plan] for plan in plans]
	requests = []
	now = 0.0
	while True:
		now += rng.expovariate(config.arrival_rate)
		if now >= config.duration:
			break
		plan = rng.choices(plans, weights=weights, k=1)[0]
		lifetime = rng.expovariate(1.0 / config.mean_lifetime)
		upgrade_offsets = []
		if config.upgrade_hazard > 0:
			offset = 0.0
			while True:
				offset += rng.expovariate(config.upgrade_hazard)
				if offset >= lifetime:
					break
				upgrade_offsets.append(offset)
		requests.append(_Request(now, plan, lifetime, upgrade_offsets))
	return requests


class _Stats:
	def __init__(self) -> None:
		self.accepted = 0
		self.blocked = 0
		self.attempts_by_plan = {plan: 0 for plan in _PLANS}
		self.blocked_by_plan = {plan: 0 for plan in _PLANS}
		self.in_place_upgrades = 0
		self.forced_migrations = 0
		self.blocked_resizes = 0
		self.peak_hosts_in_use = 0
		self._util = {axis: 0.0 for axis in packing.AXES}
		self._hosts_in_use = 0.0
		self._compaction = 0.0

	def sample(self, hosts, dt: float) -> None:
		if dt <= 0:
			return
		utilization = packing_metrics.committed_utilization(hosts)
		for axis in packing.AXES:
			self._util[axis] += utilization[axis] * dt
		in_use = sum(1 for host in hosts if host.vm_count)
		self._hosts_in_use += in_use * dt
		self._compaction += packing_metrics.compaction_ratio(hosts) * dt
		self.peak_hosts_in_use = max(self.peak_hosts_in_use, in_use)

	def finalize(self, duration: float) -> dict:
		attempted = self.accepted + self.blocked
		upgrade_attempts = self.in_place_upgrades + self.forced_migrations + self.blocked_resizes
		return {
			"accepted": self.accepted,
			"attempted": attempted,
			"acceptance": self.accepted / attempted if attempted else 1.0,
			"blocked": self.blocked,
			"blocked_largest_rate": (
				self.blocked_by_plan[_LARGEST_PLAN] / self.attempts_by_plan[_LARGEST_PLAN]
				if self.attempts_by_plan[_LARGEST_PLAN]
				else 0.0
			),
			"in_place_upgrade_rate": (self.in_place_upgrades / upgrade_attempts if upgrade_attempts else 1.0),
			"forced_migrations": self.forced_migrations,
			"blocked_resizes": self.blocked_resizes,
			"mean_utilization": {axis: self._util[axis] / duration for axis in packing.AXES},
			"mean_hosts_in_use": self._hosts_in_use / duration,
			"peak_hosts_in_use": self.peak_hosts_in_use,
			"mean_compaction": self._compaction / duration,
		}


class _Simulation:
	def __init__(self, config: SimConfig, workload: list) -> None:
		self.config = config
		self.workload = workload
		self.hosts = [
			_Host(index, config.host_shapes[index % len(config.host_shapes)])
			for index in range(config.host_count)
		]
		self.stats = _Stats()
		self._events: list = []  # heap of (time, seq, kind, payload)
		self._seq = 0
		self._clock = 0.0

	def _push(self, time: float, kind: str, payload) -> None:
		heapq.heappush(self._events, (time, self._seq, kind, payload))
		self._seq += 1

	def _place(self, demand: dict, exclude: "_Host | None" = None) -> "_Host | None":
		best = None
		for index, host in enumerate(self.hosts):
			if host is exclude:
				continue
			key = packing.rank_key(
				self.config.strategy, host.budget, host.used, demand, self.config.reserve, index
			)
			if key is None:
				continue
			if best is None or key < best[0]:
				best = (key, host)
		return best[1] if best else None

	def run(self) -> dict:
		for request in self.workload:
			self._push(request.arrival, "arrival", request)
		while self._events:
			time, _seq, kind, payload = heapq.heappop(self._events)
			if time >= self.config.duration:
				break
			self.stats.sample(self.hosts, time - self._clock)
			self._clock = time
			if kind == "arrival":
				self._on_arrival(payload)
			elif kind == "depart":
				self._on_depart(payload)
			else:
				self._on_upgrade(payload)
		# Account the final steady-state segment up to the horizon. Events scheduled
		# beyond it (drain-down departs/upgrades) are dropped: they don't affect the
		# [0, duration) time-averaged metrics, and every arrival is < duration by
		# construction, so acceptance/blocked are already complete.
		self.stats.sample(self.hosts, self.config.duration - self._clock)
		return self.stats.finalize(self.config.duration)

	def _on_arrival(self, request: "_Request") -> None:
		self.stats.attempts_by_plan[request.plan] += 1
		demand = plan_demand(request.plan)
		host = self._place(demand)
		if host is None:
			self.stats.blocked += 1
			self.stats.blocked_by_plan[request.plan] += 1
			return
		host.add(demand)
		self.stats.accepted += 1
		vm = _VM(request.plan, demand, host)
		self._push(request.arrival + request.lifetime, "depart", vm)
		for offset in request.upgrade_offsets:
			self._push(request.arrival + offset, "upgrade", vm)

	def _on_depart(self, vm: "_VM") -> None:
		if not vm.alive:
			return
		vm.host.remove(vm.demand)
		vm.alive = False

	def _on_upgrade(self, vm: "_VM") -> None:
		if not vm.alive:
			return
		target = _next_plan(vm.plan)
		if target is None:
			return
		new = plan_demand(target)
		delta = {axis: new[axis] - vm.demand[axis] for axis in packing.AXES}
		if vm.host.in_place_fits(delta):
			for axis in packing.AXES:
				vm.host.used[axis] += delta[axis]
			vm.plan, vm.demand = target, new
			self.stats.in_place_upgrades += 1
			return
		# No room to grow in place → the resize needs a migration (spec/28 case 2).
		source = vm.host
		source.remove(vm.demand)
		target_host = self._place(new, exclude=source)
		if target_host is not None:
			target_host.add(new)
			vm.host, vm.plan, vm.demand = target_host, target, new
			self.stats.forced_migrations += 1
		else:
			source.add(vm.demand)  # roll back — the VM keeps its old size on its old host
			self.stats.blocked_resizes += 1


def run(config: SimConfig) -> dict:
	"""Run one strategy over a fresh (seeded) workload; return the finalized stats."""
	return _Simulation(config, generate_workload(config)).run()


def compare(config: SimConfig, strategies=packing.STRATEGIES) -> dict:
	"""Run every strategy over the SAME workload (fair, reproducible). `config.seed`
	fixes the workload; the strategy field is overridden per run."""
	workload = generate_workload(config)
	results = {}
	for strategy in strategies:
		run_config = SimConfig(**{**config.__dict__, "strategy": strategy})
		results[strategy] = _Simulation(run_config, workload).run()
	return results


_SCALAR_METRICS = (
	"acceptance",
	"blocked_largest_rate",
	"in_place_upgrade_rate",
	"forced_migrations",
	"mean_hosts_in_use",
	"mean_compaction",
)


def compare_seeds(config: SimConfig, seeds: int, strategies=packing.STRATEGIES) -> dict:
	"""Run `compare` over `seeds` independent seeds and aggregate mean + sample stddev
	per strategy for the scalar metrics — the statistical view that says whether a
	strategy's edge is real or one lucky workload. Each seed's workload is still shared
	across strategies (fair), and the whole sweep is reproducible from `config.seed`."""
	runs = [
		compare(SimConfig(**{**config.__dict__, "seed": config.seed + index}), strategies)
		for index in range(seeds)
	]
	aggregate = {}
	for strategy in strategies:
		per_metric = {}
		for metric in _SCALAR_METRICS:
			values = [run[strategy][metric] for run in runs]
			stddev = statistics.stdev(values) if len(values) > 1 else 0.0
			per_metric[metric] = (statistics.fmean(values), stddev)
		aggregate[strategy] = per_metric
	return aggregate


def _format_aggregate(aggregate: dict, seeds: int) -> str:
	rows = [("strategy", "accept", "blk-lrg", "in-place", "migrations (mean±sd)", "hosts", "compaction")]
	for strategy, metrics in aggregate.items():
		migrations_mean, migrations_sd = metrics["forced_migrations"]
		rows.append(
			(
				strategy,
				f"{metrics['acceptance'][0]:.3f}",
				f"{metrics['blocked_largest_rate'][0]:.3f}",
				f"{metrics['in_place_upgrade_rate'][0]:.3f}",
				f"{migrations_mean:.1f} ± {migrations_sd:.1f}",
				f"{metrics['mean_hosts_in_use'][0]:.1f}",
				f"{metrics['mean_compaction'][0]:.3f}",
			)
		)
	widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
	table = "\n".join("  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)) for row in rows)
	return f"averaged over {seeds} seeds\n{table}"


def _format_table(results: dict) -> str:
	rows = [
		("strategy", "accept", "blk-lrg", "in-place", "migrations", "util(mem)", "hosts", "compaction"),
	]
	for strategy, stats in results.items():
		rows.append(
			(
				strategy,
				f"{stats['acceptance']:.3f}",
				f"{stats['blocked_largest_rate']:.3f}",
				f"{stats['in_place_upgrade_rate']:.3f}",
				str(stats["forced_migrations"]),
				f"{stats['mean_utilization']['memory']:.3f}",
				f"{stats['mean_hosts_in_use']:.1f}",
				f"{stats['mean_compaction']:.3f}",
			)
		)
	widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
	return "\n".join("  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)) for row in rows)


def _parse_shape(text: str) -> tuple:
	cpu, memory, disk = (float(part) for part in text.split(","))
	return (cpu, memory, disk)


def _build_config(ns: argparse.Namespace) -> SimConfig:
	shapes = tuple(_parse_shape(shape) for shape in ns.host_shape) if ns.host_shape else (DEFAULT_HOST_SHAPE,)
	return SimConfig(
		seed=ns.seed,
		duration=ns.duration,
		arrival_rate=ns.arrival_rate,
		host_count=ns.host_count,
		host_shapes=shapes,
		mean_lifetime=ns.mean_lifetime,
		upgrade_hazard=ns.upgrade_hazard,
		reserve=ns.reserve / 100.0,
	)


def main(argv=None) -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--seed", type=int, default=0)
	parser.add_argument("--seeds", type=int, default=1, help="average over this many seeds (mean ± sd)")
	parser.add_argument("--duration", type=float, default=10000.0, help="sim-time units")
	parser.add_argument("--arrival-rate", type=float, default=1.0, help="Poisson arrivals per unit time")
	parser.add_argument("--host-count", type=int, default=20)
	parser.add_argument(
		"--host-shape",
		action="append",
		default=[],
		metavar="CPU,MEM_MB,DISK_GB",
		help="host effective budget; repeat for a heterogeneous fleet (cycled)",
	)
	parser.add_argument("--mean-lifetime", type=float, default=500.0)
	parser.add_argument(
		"--upgrade-hazard", type=float, default=0.0, help="resize-up rate per VM per unit time"
	)
	parser.add_argument("--reserve", type=float, default=0.0, help="arrival headroom percent")
	parser.add_argument(
		"--strategy",
		choices=packing.STRATEGIES,
		help="run only this strategy (default: compare all)",
	)
	args = parser.parse_args(argv)
	config = _build_config(args)
	strategies = (args.strategy,) if args.strategy else packing.STRATEGIES
	if args.seeds > 1:
		print(_format_aggregate(compare_seeds(config, args.seeds, strategies), args.seeds))
	elif args.strategy:
		print(
			_format_table({args.strategy: run(SimConfig(**{**config.__dict__, "strategy": args.strategy}))})
		)
	else:
		print(_format_table(compare(config)))


if __name__ == "__main__":
	main()
