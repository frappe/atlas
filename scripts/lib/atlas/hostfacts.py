"""Host capacity facts — the three physical totals placement packs against.

A host reports three numbers about itself: how many CPU threads it has, how much
RAM, and how big the LVM thin pool its VM disks draw from is (plus the pool's live
fullness). Bootstrap stamps the totals once (they ride the `BootstrapResult`
line), and the `server-facts` Task re-reads them on demand — the **Refresh
Capacity** button — without a full re-bootstrap.

The parse functions are pure (text in, number out) so they unit-test on any
machine with fixture text; only `host_capacity_facts()` touches the live host
(procfs, `os.cpu_count`, and the thin pool via `atlas.lvm.ThinPool`).
"""

from __future__ import annotations

import os

from atlas.lvm import ThinPool

MEMINFO_PATH = "/proc/meminfo"


def parse_memory_megabytes(meminfo_text: str) -> int:
	"""Physical RAM in MB from `/proc/meminfo`'s `MemTotal:` line (reported in kB).

	Integer division to MB matches how the memory axis is accounted everywhere else
	(whole MB); the sub-MB remainder is host overhead the memory reserve covers."""
	for line in meminfo_text.splitlines():
		key, _, rest = line.partition(":")
		if key.strip() == "MemTotal":
			return int(rest.strip().split()[0]) // 1024
	raise ValueError(f"no MemTotal line in {MEMINFO_PATH}")


def host_capacity_facts() -> dict:
	"""The live host's capacity totals + pool fullness, as the controller stamps them.

	`vcpus_total` is the logical CPU count; `memory_megabytes_total` is physical RAM;
	`pool_disk_gigabytes_total` is the thin pool's data capacity; `pool_data_percent`
	is its live fill (advisory alert signal, never a placement predicate)."""
	# nosemgrep: frappe-security-file-traversal -- host script; reads the fixed MEMINFO_PATH (/proc/meminfo), not web input
	with open(MEMINFO_PATH) as handle:
		memory_megabytes = parse_memory_megabytes(handle.read())
	pool = ThinPool()
	return {
		"vcpus_total": os.cpu_count() or 0,
		"memory_megabytes_total": memory_megabytes,
		"pool_disk_gigabytes_total": pool.size_bytes // (1024**3),
		"pool_data_percent": pool.usage.data_percent,
	}
