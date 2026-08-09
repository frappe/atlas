"""Generic (arbitrary-shape) packing evaluation for PR #30.

The plan's headline insight — every size is a scalar multiple of one share unit, so
packing is one-dimensional and all strategies tie — holds ONLY for the proportional
ladder. Real tenants are not proportional: an object-store VM is huge disk / tiny
CPU+RAM, a miner is huge CPU / tiny everything-else, an in-memory cache is huge RAM.
Those shapes make packing genuinely multi-dimensional, so cross-axis stranding is
real and the strategies diverge. This exercises the SAME production scorer
(`atlas.atlas.packing.rank_key`) with arbitrary demand vectors — the honest test of
which strategy to default to when Custom shapes are in the mix.
"""

from __future__ import annotations

import heapq
import os
import random
import statistics
import sys

# repo root (…/llm/eval/<this>.py → three levels up), so `atlas.atlas.*` imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.atlas import packing, packing_metrics

AXES = packing.AXES

# Heterogeneous fleet mirroring the 3 real droplets I provision (EFFECTIVE budgets:
# CPU x overprovision=1, memory minus the 1024 MB host floor, pool disk after OS).
SMALL = {"cpu": 2.0, "memory": 3072.0, "disk": 70.0}  # s-2vcpu-4gb
MEDIUM = {"cpu": 4.0, "memory": 7168.0, "disk": 150.0}  # s-4vcpu-8gb
LARGE = {"cpu": 8.0, "memory": 15360.0, "disk": 300.0}  # s-8vcpu-16gb
FLEET = (SMALL, MEDIUM, LARGE)

# Arbitrary tenant archetypes: (cpu_cores, memory_mb, disk_gb) base shape + which axis
# they grow when they resize. Deliberately NON-proportional — each is heavy on a
# different axis, so no single host shape packs them all without stranding.
ARCHETYPES = {
	#                     cpu    mem     disk   grows
	"object_store": ((0.25, 1024.0, 80.0), "disk"),  # huge disk, tiny cpu/ram
	"miner": ((3.0, 512.0, 10.0), "cpu"),  # huge cpu, tiny ram/disk
	"cache": ((0.5, 6144.0, 20.0), "memory"),  # huge ram
	"web": ((0.5, 1024.0, 20.0), "cpu"),  # small balanced-ish
	"batch": ((1.5, 3072.0, 60.0), "cpu"),  # bigger balanced
}
# Skewed mix: lots of small web + the three lopsided specialists + some batch.
ARCH_MIX = {"object_store": 2.0, "miner": 2.0, "cache": 2.0, "web": 4.0, "batch": 1.0}
# The "canary" archetypes whose blocked-rate signals cross-axis fragmentation.
LOPSIDED = ("object_store", "miner", "cache")


def _jitter(rng, base, lo=0.7, hi=1.6):
	"""Per-instance size jitter so shapes are genuinely arbitrary, not 5 fixed points."""
	return {axis: base[i] * rng.uniform(lo, hi) for i, axis in enumerate(AXES)}


class Host:
	def __init__(self, hid, shape):
		self.host_id = hid
		self.budget = dict(shape)
		self.used = {a: 0.0 for a in AXES}
		self.vm_count = 0

	def add(self, d):
		for a in AXES:
			self.used[a] += d[a]
		self.vm_count += 1

	def remove(self, d):
		for a in AXES:
			self.used[a] -= d[a]
		self.vm_count -= 1

	def fits_full(self, delta):
		return all(self.used[a] + delta[a] <= self.budget[a] for a in AXES)


class VM:
	__slots__ = ("alive", "arch", "demand", "host")

	def __init__(self, arch, demand, host):
		self.arch = arch
		self.demand = demand
		self.host = host
		self.alive = True


def gen_workload(cfg):
	rng = random.Random(cfg["seed"])
	archs = list(ARCH_MIX)
	weights = [ARCH_MIX[a] for a in archs]
	reqs = []
	now = 0.0
	while True:
		now += rng.expovariate(cfg["arrival_rate"])
		if now >= cfg["duration"]:
			break
		arch = rng.choices(archs, weights=weights, k=1)[0]
		base, grow = ARCHETYPES[arch]
		demand = _jitter(rng, base)
		lifetime = rng.expovariate(1.0 / cfg["mean_lifetime"])
		ups = []
		if cfg["upgrade_hazard"] > 0:
			off = 0.0
			while True:
				off += rng.expovariate(cfg["upgrade_hazard"])
				if off >= lifetime:
					break
				ups.append(off)
		reqs.append((now, arch, demand, grow, lifetime, ups))
	return reqs


def place(hosts, strategy, demand, reserve, exclude=None):
	best = None
	for idx, h in enumerate(hosts):
		if h is exclude:
			continue
		key = packing.rank_key(strategy, h.budget, h.used, demand, reserve, idx)
		if key is None:
			continue
		if best is None or key < best[0]:
			best = (key, h)
	return best[1] if best else None


def simulate(cfg, workload):
	hosts = [Host(i, FLEET[i % len(FLEET)]) for i in range(cfg["host_count"])]
	strat = cfg["strategy"]
	reserve = cfg["reserve"]
	events = []
	seq = 0

	def push(t, kind, payload):
		nonlocal seq
		heapq.heappush(events, (t, seq, kind, payload))
		seq += 1

	for t, arch, demand, grow, lifetime, ups in workload:
		push(t, "arrival", (arch, demand, grow, lifetime, ups))

	accepted = blocked = 0
	att = {a: 0 for a in ARCH_MIX}
	blk = {a: 0 for a in ARCH_MIX}
	in_place = forced = blocked_resize = 0
	util = {a: 0.0 for a in AXES}
	imbalance_acc = hosts_in_use_acc = compaction_acc = 0.0
	clock = 0.0

	def sample(dt):
		nonlocal imbalance_acc, hosts_in_use_acc, compaction_acc
		if dt <= 0:
			return
		u = packing_metrics.committed_utilization(hosts)
		for a in AXES:
			util[a] += u[a] * dt
		live = [h for h in hosts if h.vm_count]
		hosts_in_use_acc += len(live) * dt
		if live:
			imbalance_acc += statistics.fmean(packing_metrics.imbalance(h) for h in live) * dt
		compaction_acc += packing_metrics.compaction_ratio(hosts) * dt

	while events:
		t, _s, kind, payload = heapq.heappop(events)
		if t >= cfg["duration"]:
			break
		sample(t - clock)
		clock = t
		if kind == "arrival":
			arch, demand, grow, lifetime, ups = payload
			att[arch] += 1
			h = place(hosts, strat, demand, reserve)
			if h is None:
				blocked += 1
				blk[arch] += 1
				continue
			h.add(demand)
			accepted += 1
			vm = VM(arch, demand, h)
			push(t + lifetime, "depart", vm)
			for off in ups:
				push(t + off, "upgrade", vm)
		elif kind == "depart":
			vm = payload
			if vm.alive:
				vm.host.remove(vm.demand)
				vm.alive = False
		else:  # upgrade: grow the archetype's dominant axis ~1.6x
			vm = payload
			if not vm.alive:
				continue
			_base, grow = ARCHETYPES[vm.arch]
			new = dict(vm.demand)
			new[grow] = vm.demand[grow] * 1.6
			delta = {a: new[a] - vm.demand[a] for a in AXES}
			if vm.host.fits_full(delta):
				for a in AXES:
					vm.host.used[a] += delta[a]
				vm.demand = new
				in_place += 1
			else:
				src = vm.host
				src.remove(vm.demand)
				tgt = place(hosts, strat, new, reserve, exclude=src)
				if tgt is not None:
					tgt.add(new)
					vm.host, vm.demand = tgt, new
					forced += 1
				else:
					src.add(vm.demand)
					blocked_resize += 1

	sample(cfg["duration"] - clock)
	dur = cfg["duration"]
	attempted = accepted + blocked
	up_att = in_place + forced + blocked_resize
	lop_att = sum(att[a] for a in LOPSIDED)
	lop_blk = sum(blk[a] for a in LOPSIDED)
	return {
		"acceptance": accepted / attempted if attempted else 1.0,
		"blocked_lopsided_rate": lop_blk / lop_att if lop_att else 0.0,
		"blk_by_arch": {a: (blk[a] / att[a] if att[a] else 0.0) for a in ARCH_MIX},
		"in_place_rate": in_place / up_att if up_att else 1.0,
		"forced_migrations": forced,
		"mean_util": {a: util[a] / dur for a in AXES},
		"mean_imbalance": imbalance_acc / dur,
		"mean_hosts_in_use": hosts_in_use_acc / dur,
		"mean_compaction": compaction_acc / dur,
	}


def compare_seeds(base_cfg, seeds):
	agg = {s: {} for s in packing.STRATEGIES}
	runs = []
	for i in range(seeds):
		cfg = dict(base_cfg, seed=base_cfg["seed"] + i)
		workload = gen_workload(cfg)
		per = {}
		for strat in packing.STRATEGIES:
			per[strat] = simulate(dict(cfg, strategy=strat), workload)
		runs.append(per)
	scalars = (
		"acceptance",
		"blocked_lopsided_rate",
		"in_place_rate",
		"forced_migrations",
		"mean_imbalance",
		"mean_hosts_in_use",
		"mean_compaction",
	)
	for strat in packing.STRATEGIES:
		for m in scalars:
			vals = [r[strat][m] for r in runs]
			sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
			agg[strat][m] = (statistics.fmean(vals), sd)
		# per-archetype block rate (mean over seeds)
		agg[strat]["blk_by_arch"] = {
			a: statistics.fmean(r[strat]["blk_by_arch"][a] for r in runs) for a in ARCH_MIX
		}
	return agg


def main():
	seeds = 24
	for rate, reserve in ((0.20, 0.0), (0.28, 0.0), (0.36, 0.0), (0.28, 0.10)):
		base = {
			"seed": 500,
			"duration": 20000.0,
			"arrival_rate": rate,
			"host_count": 12,
			"mean_lifetime": 500.0,
			"upgrade_hazard": 0.002,
			"reserve": reserve,
		}
		agg = compare_seeds(base, seeds)
		print(
			f"\n=== arrival_rate={rate}  reserve={int(reserve * 100)}%  (arbitrary shapes, {seeds} seeds) ==="
		)
		print(
			f"{'strategy':<10} {'accept':>14} {'blk-lopsided':>13} {'in-place':>9} "
			f"{'migr':>12} {'imbal':>7} {'hosts':>6} {'compact':>8}"
		)
		for s in packing.STRATEGIES:
			m = agg[s]
			print(
				f"{s:<10} {m['acceptance'][0]:.3f}±{m['acceptance'][1]:.3f}   "
				f"{m['blocked_lopsided_rate'][0]:>11.3f} {m['in_place_rate'][0]:>9.3f} "
				f"{m['forced_migrations'][0]:>7.0f}±{m['forced_migrations'][1]:<4.0f} "
				f"{m['mean_imbalance'][0]:>7.3f} {m['mean_hosts_in_use'][0]:>6.1f} "
				f"{m['mean_compaction'][0]:>8.3f}"
			)
		# per-archetype blocked rate at the middle load, no reserve
		if rate == 0.28 and reserve == 0.0:
			print("  per-archetype blocked rate:")
			for s in packing.STRATEGIES:
				ba = agg[s]["blk_by_arch"]
				print(f"    {s:<10} " + "  ".join(f"{a}={ba[a]:.3f}" for a in ARCH_MIX))


if __name__ == "__main__":
	main()
