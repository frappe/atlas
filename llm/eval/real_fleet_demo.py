"""Capstone: run PR #30's production scorer against the 3 REAL droplets just
provisioned (4/8/16 GB), placing arbitrary-shaped VMs under each strategy.

Effective budgets = DO size minus the host_memory_reserve (1024 MB) on RAM,
overprovision_factor 1.0 on CPU, pool disk after OS. Mirrors what
capacity_for_server would report on a PR #30 host.
"""

import os
import sys

# repo root (…/llm/eval/<this>.py → three levels up), so `atlas.atlas.*` imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas.atlas import packing, packing_metrics

AXES = packing.AXES

# The three real Active droplets (title -> effective per-axis budget).
FLEET = [
	("x-Pune-placement-41cc45 (4GB)", {"cpu": 2.0, "memory": 3072.0, "disk": 70.0}),
	("x-Pune-placement-fe7e76 (8GB)", {"cpu": 4.0, "memory": 7168.0, "disk": 150.0}),
	("x-Pune-placement-26875b (16GB)", {"cpu": 8.0, "memory": 15360.0, "disk": 300.0}),
]

# Arbitrary tenant VMs, deliberately non-proportional (cpu cores, mem MB, disk GB).
ARRIVALS = [
	("object-store-1", {"cpu": 0.25, "memory": 1024.0, "disk": 80.0}),  # huge disk
	("miner-1", {"cpu": 3.0, "memory": 512.0, "disk": 10.0}),  # huge cpu
	("cache-1", {"cpu": 0.5, "memory": 6144.0, "disk": 20.0}),  # huge ram
	("web-1", {"cpu": 0.5, "memory": 1024.0, "disk": 20.0}),
	("web-2", {"cpu": 0.5, "memory": 1024.0, "disk": 20.0}),
	("batch-1", {"cpu": 1.5, "memory": 3072.0, "disk": 60.0}),
	("cache-2", {"cpu": 0.5, "memory": 6144.0, "disk": 20.0}),
	("object-store-2", {"cpu": 0.25, "memory": 1024.0, "disk": 60.0}),
]


class H:
	def __init__(self, name, budget):
		self.name = name
		self.budget = dict(budget)
		self.used = {a: 0.0 for a in AXES}
		self.vm_count = 0


def place(hosts, strategy, needs, reserve=0.0):
	best = None
	for idx, h in enumerate(hosts):
		key = packing.rank_key(strategy, h.budget, h.used, needs, reserve, idx)
		if key is None:
			continue
		if best is None or key < best[0]:
			best = (key, h)
	return best[1] if best else None


def run(strategy):
	hosts = [H(n, b) for n, b in FLEET]
	print(f"\n{'=' * 66}\nStrategy: {strategy}\n{'=' * 66}")
	for vm, needs in ARRIVALS:
		h = place(hosts, strategy, needs)
		shape = f"cpu={needs['cpu']:.2f} mem={int(needs['memory'])}MB disk={int(needs['disk'])}GB"
		if h is None:
			print(f"  {vm:<15} {shape:<38} -> BLOCKED (no host fits)")
			continue
		h.add(needs) if False else None
		for a in AXES:
			h.used[a] += needs[a]
		h.vm_count += 1
		print(f"  {vm:<15} {shape:<38} -> {h.name}")
	print("  " + "-" * 62)
	for h in hosts:
		fill = {a: (h.used[a] / h.budget[a]) if h.budget[a] else 0 for a in AXES}
		su = packing_metrics.share_units(h)
		print(
			f"  {h.name:<32} vms={h.vm_count}  "
			f"fill cpu={fill['cpu'] * 100:3.0f}% mem={fill['memory'] * 100:3.0f}% disk={fill['disk'] * 100:3.0f}%  "
			f"units {su['used']}/{su['total']}"
		)


if __name__ == "__main__":
	for strat in (packing.SPREAD, packing.BEST_FIT, packing.TETRIS):
		run(strat)
