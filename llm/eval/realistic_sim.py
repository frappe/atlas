"""Realistic composite workload for the PR #30 packing bake-off.

The regime real fleets actually see (and the two earlier sims did NOT cover):
  * MANY small VMs of a FIXED shape (the ladder's Shared 1x/2x) — ~80% of arrivals,
  * a FEW arbitrary-sized lopsided ones (object-store/miner/cache/batch) — ~20%,
  * random DROPS (exp lifetimes),
  * random RESIZES (upgrade hazard → in-place / forced-migration / blocked),
  * random ad-hoc MIGRATIONS independent of resize (host drain / rebalance / evict):
    a per-VM hazard that tries to move the VM to a DIFFERENT host via the scorer.
    Its failure rate = "evacuation failure" = can I drain a host on demand? — the
    metric that actually tests Best Fit's claimed 'keeps hosts drainable' virtue.

Everything is scored through the SAME production scorer (`atlas.atlas.packing.rank_key`).
"""

import heapq
import os
import random
import statistics
import sys

# repo root (…/llm/eval/<this>.py → three levels up), so `atlas.atlas.*` imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.atlas import packing, packing_metrics
from atlas.atlas.sizes import SIZE_PRESETS

AXES = packing.AXES

SMALL = {"cpu": 2.0, "memory": 3072.0, "disk": 70.0}  # s-2vcpu-4gb effective
MEDIUM = {"cpu": 4.0, "memory": 7168.0, "disk": 150.0}  # s-4vcpu-8gb
LARGE = {"cpu": 8.0, "memory": 15360.0, "disk": 300.0}  # s-8vcpu-16gb
FLEET = (SMALL, MEDIUM, LARGE)

FIXED_LADDER = ["Shared 1x", "Shared 2x", "Shared 4x", "Shared 8x", "Dedicated 1x"]


def preset_demand(name):
	p = SIZE_PRESETS[name]
	return {
		"cpu": float(p["cpu_max_cores"]),
		"memory": float(p["memory_megabytes"]),
		"disk": float(p["disk_gigabytes"]),
	}


# archetype -> (base cpu,mem,disk), dominant grow axis
ARCH = {
	"object_store": ((0.25, 1024.0, 80.0), "disk"),
	"miner": ((3.0, 512.0, 10.0), "cpu"),
	"cache": ((0.5, 6144.0, 20.0), "memory"),
	"batch": ((1.5, 3072.0, 60.0), "cpu"),
}
LOPSIDED = tuple(ARCH)
# ~80% small fixed shapes, ~20% arbitrary lopsided.
MIX = {"Shared 1x": 8.0, "Shared 2x": 4.0, "object_store": 0.75, "miner": 0.75, "cache": 0.75, "batch": 0.75}


def jitter(rng, base, lo=0.7, hi=1.6):
	return {a: base[i] * rng.uniform(lo, hi) for i, a in enumerate(AXES)}


class Host:
	def __init__(self, hid, shape):
		self.hid = hid
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
	__slots__ = ("alive", "demand", "host", "kind", "ladder_i")

	def __init__(self, kind, demand, host, ladder_i):
		self.kind = kind
		self.demand = demand
		self.host = host
		self.alive = True
		self.ladder_i = ladder_i  # index into FIXED_LADDER, or None for arbitrary


def gen_workload(cfg):
	rng = random.Random(cfg["seed"])
	kinds = list(MIX)
	weights = [MIX[k] for k in kinds]
	reqs = []
	now = 0.0
	while True:
		now += rng.expovariate(cfg["arrival_rate"])
		if now >= cfg["duration"]:
			break
		kind = rng.choices(kinds, weights=weights, k=1)[0]
		if kind in ARCH:
			demand = jitter(rng, ARCH[kind][0])
			ladder_i = None
		else:
			demand = preset_demand(kind)
			ladder_i = FIXED_LADDER.index(kind)
		lifetime = rng.expovariate(1.0 / cfg["mean_lifetime"])
		ups = _thin(rng, cfg["upgrade_hazard"], lifetime)
		migs = _thin(rng, cfg["migration_hazard"], lifetime)
		reqs.append((now, kind, demand, ladder_i, lifetime, ups, migs))
	return reqs


def _thin(rng, hazard, lifetime):
	out = []
	if hazard > 0:
		off = 0.0
		while True:
			off += rng.expovariate(hazard)
			if off >= lifetime:
				break
			out.append(off)
	return out


def place(hosts, strat, needs, reserve, exclude=None):
	best = None
	for idx, h in enumerate(hosts):
		if h is exclude:
			continue
		key = packing.rank_key(strat, h.budget, h.used, needs, reserve, idx)
		if key is None:
			continue
		if best is None or key < best[0]:
			best = (key, h)
	return best[1] if best else None


def simulate(cfg, workload):
	hosts = [Host(i, FLEET[i % len(FLEET)]) for i in range(cfg["host_count"])]
	strat, reserve = cfg["strategy"], cfg["reserve"]
	ev, seq, clock = [], 0, 0.0

	def push(t, kind, p):
		nonlocal seq
		heapq.heappush(ev, (t, seq, kind, p))
		seq += 1

	for t, kind, demand, ladder_i, lifetime, ups, migs in workload:
		push(t, "arr", (kind, demand, ladder_i, lifetime, ups, migs))

	acc = blk = 0
	att = {k: 0 for k in MIX}
	bk = {k: 0 for k in MIX}
	in_place = forced = blk_resize = 0
	rand_migr = evac_fail = 0
	util = {a: 0.0 for a in AXES}
	imb = huse = 0.0

	def sample(dt):
		nonlocal imb, huse
		if dt <= 0:
			return
		u = packing_metrics.committed_utilization(hosts)
		for a in AXES:
			util[a] += u[a] * dt
		live = [h for h in hosts if h.vm_count]
		huse += len(live) * dt
		if live:
			imb += statistics.fmean(packing_metrics.imbalance(h) for h in live) * dt

	while ev:
		t, _s, kind, p = heapq.heappop(ev)
		if t >= cfg["duration"]:
			break
		sample(t - clock)
		clock = t
		if kind == "arr":
			k, demand, ladder_i, lifetime, ups, migs = p
			att[k] += 1
			h = place(hosts, strat, demand, reserve)
			if h is None:
				blk += 1
				bk[k] += 1
				continue
			h.add(demand)
			acc += 1
			vm = VM(k, demand, h, ladder_i)
			push(t + lifetime, "dep", vm)
			for o in ups:
				push(t + o, "up", vm)
			for o in migs:
				push(t + o, "mig", vm)
		elif kind == "dep":
			vm = p
			if vm.alive:
				vm.host.remove(vm.demand)
				vm.alive = False
		elif kind == "up":
			vm = p
			if not vm.alive:
				continue
			new = _grow(vm)
			if new is None:
				continue
			delta = {a: new[a] - vm.demand[a] for a in AXES}
			if vm.host.fits_full(delta):
				for a in AXES:
					vm.host.used[a] += delta[a]
				vm.demand = new
				if vm.ladder_i is not None:
					vm.ladder_i += 1
				in_place += 1
			else:
				src = vm.host
				src.remove(vm.demand)
				tgt = place(hosts, strat, new, reserve, exclude=src)
				if tgt is not None:
					tgt.add(new)
					vm.host, vm.demand = tgt, new
					if vm.ladder_i is not None:
						vm.ladder_i += 1
					forced += 1
				else:
					src.add(vm.demand)
					blk_resize += 1
		else:  # mig: ad-hoc random migration (drain/rebalance/evict this VM elsewhere)
			vm = p
			if not vm.alive:
				continue
			src = vm.host
			src.remove(vm.demand)
			tgt = place(hosts, strat, vm.demand, reserve, exclude=src)
			if tgt is not None:
				tgt.add(vm.demand)
				vm.host = tgt
				rand_migr += 1
			else:
				src.add(vm.demand)  # nowhere to evacuate to → stays put
				evac_fail += 1

	sample(cfg["duration"] - clock)
	dur = cfg["duration"]
	attempted = acc + blk
	up_att = in_place + forced + blk_resize
	lop_att = sum(att[k] for k in LOPSIDED)
	lop_blk = sum(bk[k] for k in LOPSIDED)
	mig_att = rand_migr + evac_fail
	return {
		"acceptance": acc / attempted if attempted else 1.0,
		"blocked_lopsided_rate": lop_blk / lop_att if lop_att else 0.0,
		"in_place_rate": in_place / up_att if up_att else 1.0,
		"forced_migrations": forced,
		"evac_fail_rate": evac_fail / mig_att if mig_att else 0.0,
		"random_migrations": rand_migr,
		"mean_imbalance": imb / dur,
		"mean_hosts_in_use": huse / dur,
	}


def _grow(vm):
	if vm.ladder_i is not None:
		nxt = vm.ladder_i + 1
		if nxt >= len(FIXED_LADDER):
			return None
		return preset_demand(FIXED_LADDER[nxt])
	grow = ARCH[vm.kind][1]
	new = dict(vm.demand)
	new[grow] = vm.demand[grow] * 1.6
	return new


def compare_seeds(base, seeds):
	scalars = (
		"acceptance",
		"blocked_lopsided_rate",
		"in_place_rate",
		"forced_migrations",
		"evac_fail_rate",
		"random_migrations",
		"mean_imbalance",
		"mean_hosts_in_use",
	)
	runs = []
	for i in range(seeds):
		cfg = dict(base, seed=base["seed"] + i)
		wl = gen_workload(cfg)
		runs.append({s: simulate(dict(cfg, strategy=s), wl) for s in packing.STRATEGIES})
	agg = {}
	for s in packing.STRATEGIES:
		agg[s] = {}
		for m in scalars:
			vals = [r[s][m] for r in runs]
			agg[s][m] = (statistics.fmean(vals), statistics.stdev(vals) if len(vals) > 1 else 0.0)
	return agg


def main():
	seeds = 16
	for rate, reserve in ((0.3, 0.0), (0.5, 0.0), (0.7, 0.0), (0.5, 0.10)):
		base = {
			"seed": 700,
			"duration": 8000.0,
			"arrival_rate": rate,
			"host_count": 12,
			"mean_lifetime": 500.0,
			"upgrade_hazard": 0.0015,
			"migration_hazard": 0.0015,
			"reserve": reserve,
		}
		agg = compare_seeds(base, seeds)
		print(
			f"\n=== rate={rate} reserve={int(reserve * 100)}%  (80% fixed-small + 20% arbitrary; "
			f"drops+resizes+random-migrations; {seeds} seeds) ==="
		)
		print(
			f"{'strategy':<10} {'accept':>13} {'blk-lop':>8} {'in-place':>9} "
			f"{'resize-migr':>12} {'evac-fail':>10} {'rand-migr':>10} {'imbal':>7} {'hosts':>6}"
		)
		for s in packing.STRATEGIES:
			m = agg[s]
			print(
				f"{s:<10} {m['acceptance'][0]:.3f}±{m['acceptance'][1]:.3f} "
				f"{m['blocked_lopsided_rate'][0]:>8.3f} {m['in_place_rate'][0]:>9.3f} "
				f"{m['forced_migrations'][0]:>7.0f}±{m['forced_migrations'][1]:<4.0f} "
				f"{m['evac_fail_rate'][0]:>10.3f} {m['random_migrations'][0]:>10.0f} "
				f"{m['mean_imbalance'][0]:>7.3f} {m['mean_hosts_in_use'][0]:>6.1f}"
			)


if __name__ == "__main__":
	main()
