#!/usr/bin/env python3
"""Atlas host dashboard — the read-only backend.

A visual `ls`/`cat` for one Firecracker host. It reads the host's state from
disk (`/var/lib/atlas`) and from a handful of read-only host commands (`ip`,
`nft`, `systemctl`, `lvs`), assembles one JSON document, and serves it at
`/api/state` alongside the static SPA built into `dist/`.

Design constraints (mirroring the Atlas spec's principles):

- **Stdlib only.** No Frappe, no third-party packages. `http.server` +
  `subprocess` + `json`. It ships as one file next to the built frontend.
- **Read-only.** There are no write routes and no actions. Every host command
  it runs is an inspection command; it never mutates state.
- **Best-effort.** A host command that is missing or fails yields an empty
  section, never a 500 — so the same file runs on a real host and on a dev box
  with none of the tooling (where every live section is simply empty and only
  the on-disk `/var/lib/atlas` sections populate, if that path exists).

Run it on the host:

    python3 server.py            # serves 0.0.0.0:8080, static from ./dist

Point it elsewhere for local testing:

    ATLAS_ROOT=./fixture ATLAS_DIST=../dist python3 server.py
"""

from __future__ import annotations

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ATLAS_ROOT = Path(os.environ.get("ATLAS_ROOT", "/var/lib/atlas"))
DIST_DIR = Path(os.environ.get("ATLAS_DIST", Path(__file__).parent / "dist"))
BIND = os.environ.get("ATLAS_BIND", "0.0.0.0")
PORT = int(os.environ.get("ATLAS_PORT", "9797"))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def run(*argv: str) -> str:
	"""Run a read-only host command, return stdout, or '' on any failure.

	Missing binary, non-zero exit, timeout — all collapse to the empty string so
	a caller on a box without `nft`/`ip`/`lvs` simply gets an empty section.
	"""
	try:
		out = subprocess.run(
			argv,
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
		return out.stdout if out.returncode == 0 else ""
	except (OSError, subprocess.SubprocessError):
		return ""


def read_json(path: Path) -> dict:
	try:
		return json.loads(path.read_text())
	except (OSError, ValueError):
		return {}


def read_env(path: Path) -> dict:
	"""Parse a KEY=value sidecar (network.env) into a dict."""
	result = {}
	try:
		for line in path.read_text().splitlines():
			line = line.strip()
			if not line or "=" not in line or line.startswith("#"):
				continue
			key, _, value = line.partition("=")
			result[key.strip()] = value.strip()
	except OSError:
		pass
	return result


# --------------------------------------------------------------------------- #
# On-disk state: /var/lib/atlas
# --------------------------------------------------------------------------- #


def host_facts() -> dict:
	"""bootstrap.json + a few live version probes. bootstrap.json is the on-disk
	source of truth for the versions bootstrap resolved; the live probes fill in
	what it does not carry (distro string, managed package versions).

	Also carries the host's PHYSICAL totals (cpu_total, mem_total_mib) and the
	over-provision factor — the denominators the Overview quota bars divide by.
	Without them the page can show committed vCPU/RAM but not "how full is this
	host vs. its budget", which is THE operating question at scale."""
	boot = read_json(ATLAS_ROOT / "bootstrap.json")
	return {
		"hostname": socket.gethostname(),
		"collected_at": datetime.datetime.now(datetime.UTC)
		.replace(microsecond=0)
		.isoformat()
		.replace("+00:00", "Z"),
		"firecracker_version": boot.get("firecracker_version", ""),
		"jailer_version": boot.get("jailer_version", ""),
		"kernel_version": boot.get("kernel_version") or run("uname", "-r").strip(),
		"linux": _distro(),
		"architecture": boot.get("architecture") or run("uname", "-m").strip(),
		"cpu_model": _cpu_model(),
		"python_version": boot.get("python_version", ""),
		"uplink": _uplink(),
		"cpu_total": _cpu_total(),
		"mem_total_mib": _mem_total_mib(),
		"overprovision_factor": _overprovision_factor(boot),
		"packages": _packages(),
	}


def _cpu_total() -> int | None:
	"""Physical vCPU count — the quota denominator for the vCPU bar. os.cpu_count()
	reads /sys, so it works on any Linux box without shelling out."""
	try:
		return os.cpu_count()
	except OSError:
		return None


def _cpu_model() -> str:
	"""The CPU's marketing name (e.g. 'AMD EPYC 4245P 6-Core Processor') from the
	first `model name` in /proc/cpuinfo. Empty string when unreadable."""
	try:
		for line in Path("/proc/cpuinfo").read_text().splitlines():
			if line.startswith("model name"):
				return line.split(":", 1)[1].strip()
	except (OSError, IndexError):
		pass
	return ""


def _mem_total_mib() -> int | None:
	"""MemTotal from /proc/meminfo, in MiB. The RAM quota denominator."""
	try:
		for line in Path("/proc/meminfo").read_text().splitlines():
			if line.startswith("MemTotal:"):
				kib = int(line.split()[1])
				return kib // 1024
	except (OSError, ValueError, IndexError):
		pass
	return None


def _overprovision_factor(boot: dict) -> float:
	"""The host's effective budget multiplier: budget = physical x factor. From
	bootstrap.json when present (the controller writes Atlas Settings there);
	defaults to 1.0 (no over-provision) so the bars never over-report headroom."""
	try:
		return float(boot.get("overprovision_factor", 1.0)) or 1.0
	except (TypeError, ValueError):
		return 1.0


def _distro() -> str:
	"""PRETTY_NAME from /etc/os-release."""
	try:
		for line in Path("/etc/os-release").read_text().splitlines():
			if line.startswith("PRETTY_NAME="):
				return line.split("=", 1)[1].strip().strip('"')
	except OSError:
		pass
	return ""


def _uplink() -> str:
	"""The device carrying the default IPv6 route — the host's uplink."""
	out = run("ip", "-6", "route", "show", "default")
	match = re.search(r"\bdev\s+(\S+)", out)
	return match.group(1) if match else ""


def _binary_version(binary: str) -> str:
	out = run(binary, "--version")
	if not out:
		return ""
	first = out.splitlines()[0]
	tokens = first.split()
	return tokens[1] if len(tokens) > 1 else first.strip()


def _packages() -> list[dict]:
	"""The Atlas-managed package versions the operator cares about. Derived, not
	persisted — probed live from the binaries."""
	probes = [
		("firecracker", _binary_version("firecracker")),
		("nftables", _binary_version("nft")),
		("lvm2", _lvm_version()),
		("iproute2", _binary_version("ip")),
	]
	return [{"name": name, "version": version} for name, version in probes if version]


def _lvm_version() -> str:
	out = run("lvm", "version")
	match = re.search(r"LVM version:\s*(\S+)", out)
	return match.group(1) if match else ""


def virtual_machines() -> list[dict]:
	"""One row per directory under virtual-machines/, named by UUID. `ls` is the
	inventory (spec/07). Each VM's identity comes from its network.env sidecar,
	its disk from the LV naming scheme, its state from the systemd unit.

	Enriched (see CONTRACT.md) with: the host/namespace veth names and
	jailer uid (so the flat network/firewall rows join back to a VM); the VM's
	size (vcpus/mem from firecracker.json — the single most operator-relevant
	fact the old page dropped); and its disk origin + fill from one batched `lvs`
	read (so hundreds of VMs cost one command, not N). A `migrating` flag marks a
	VM mid-migration (its forwarder unit is live)."""
	base = ATLAS_ROOT / "virtual-machines"
	rows = []
	if not base.is_dir():
		return rows
	lv_info = _lvs_disk_info()  # one batched lvs read, shared across all VMs
	migrating = _migrating_uuids()
	cgroups = _vm_cgroups()  # uuid -> cgroup path, from the running fc/jailer procs
	for entry in sorted(base.iterdir()):
		if not entry.is_dir():
			continue
		uuid = entry.name
		env = read_env(entry / "network.env")
		jail_root = entry / "jail" / "firecracker" / uuid / "root"
		disk_lv = f"atlas-vm-{uuid}"
		vcpus, mem_mib = _vm_size(jail_root)
		disk = lv_info.get(disk_lv, {})
		# Committed caps (what the VM is ALLOWED) vs live actual (what it USES).
		# The gap between them is the overprovision story — a VM sized 2 vCPU /
		# 2 GiB that actually burns 3% is reclaimable headroom (spec/05 § cgroup).
		usage = _vm_cgroup_usage(cgroups.get(uuid))
		rows.append(
			{
				"uuid": uuid,
				"state": _vm_state(uuid),
				"ipv6": env.get("VIRTUAL_MACHINE_IPV6"),
				"ipv4_guest": _strip_cidr(env.get("IPV4_GUEST_CIDR")),
				"ipv4_host": _strip_cidr(env.get("IPV4_HOST_CIDR")),
				"reserved_ipv4": env.get("RESERVED_IPV4"),
				"mac": _mac_from_uuid(uuid),
				"tap_device": env.get("TAP_DEVICE"),
				"host_veth": env.get("HOST_VETH"),
				"namespace_veth": env.get("NAMESPACE_VETH"),
				"netns": env.get("ATLAS_NETNS"),
				"fc_uid": _int(env.get("ATLAS_FC_UID")),
				"private_ipv6": env.get("PRIVATE_IPV6"),  # host-mesh private /128
				"image": _vm_image(jail_root),
				# Committed size — the sizing knobs (spec/05 § resize).
				"vcpus": vcpus,
				"mem_mib": mem_mib,
				"cgroup_cpu_max": usage.get("cpu_max"),  # "quota period" (µs), or "max"
				"cgroup_memory_max": usage.get("memory_max"),  # bytes, or "max"
				# The raw cgroup caps parsed into the UI's units: the enforced ceiling
				# in cores / MiB (spec/05 § cgroup). None when unlimited ("max") or the
				# cgroup is unreadable (stopped VM / dev box).
				"cpu_cap_cores": _cpu_cap_cores(usage.get("cpu_max")),
				"mem_cap_mib": _mem_cap_mib(usage.get("memory_max")),
				# Live actual usage from the cgroup — the honest denominator for
				# "is this VM's committed size actually used?".
				"mem_used_mib": usage.get("mem_used_mib"),
				"cpu_pct": usage.get("cpu_pct"),
				"disk_lv": disk_lv,
				"disk_origin": disk.get("origin"),
				"disk_data_percent": disk.get("data_percent"),
				"disk_size_bytes": disk.get("size_bytes"),
				"disk_used_bytes": disk.get("used_bytes"),
				"has_data_disk": (jail_root / "data.ext4").exists(),
				"has_snapshot": (jail_root / "snapshot").is_dir(),
				"migrating": uuid in migrating,
				"log_size": _size(entry / "log" / "firecracker.log"),
			}
		)
	return rows


# Per-VM CPU usage needs two readings of cpu.stat to yield a percentage; like the
# host metric counters we keep the previous sample per process (keyed by cgroup
# path) so successive collections derive a real busy-fraction, not a cold spike.
_CGROUP_CPU_PREV: dict = {}


def _vm_cgroups() -> dict:
	"""Map each VM uuid → its cgroup path (relative to /sys/fs/cgroup), read from
	the live firecracker/jailer process's /proc/<pid>/cgroup. The jailer places
	each VM in its own cgroup; reading it off the process avoids hardcoding the
	jailer's slice naming. Best-effort: a stopped VM has no process, so no entry
	(its usage is simply absent — nothing runs to consume anything)."""
	out = {}
	proc = Path("/proc")
	try:
		pids = [p.name for p in proc.iterdir() if p.name.isdigit()]
	except OSError:
		return out
	for pid in pids:
		try:
			args = (proc / pid / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
		except OSError:
			continue
		if "firecracker" not in args and "jailer" not in args:
			continue
		m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", args)
		if not m:
			continue
		uuid = m.group(1)
		try:
			# cgroup-v2: a single line `0::/path`. The firecracker child is the leaf.
			for line in (proc / pid / "cgroup").read_text().splitlines():
				parts = line.split(":", 2)
				if len(parts) == 3 and parts[1] == "":
					out.setdefault(uuid, parts[2])
		except OSError:
			continue
	return out


def _vm_cgroup_usage(cgroup: str | None) -> dict:
	"""Committed caps + live actual usage for one VM's cgroup. Reads memory.max /
	memory.current and cpu.max / cpu.stat from /sys/fs/cgroup/<cgroup>. Returns
	{} when the cgroup is absent (stopped VM) or unreadable (dev box)."""
	if not cgroup:
		return {}
	root = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
	out: dict = {}

	def _read(name: str) -> str | None:
		try:
			return (root / name).read_text().strip()
		except OSError:
			return None

	mem_max = _read("memory.max")
	if mem_max is not None:
		out["memory_max"] = mem_max  # "max" (unlimited) or a byte count
	mem_cur = _read("memory.current")
	if mem_cur is not None and mem_cur.isdigit():
		out["mem_used_mib"] = int(mem_cur) // (1024 * 1024)

	cpu_max = _read("cpu.max")
	if cpu_max is not None:
		out["cpu_max"] = cpu_max  # "<quota> <period>" in µs, or "max <period>"

	# cpu.pct needs a delta of cpu.stat's usage_usec against wall time. Keep the
	# previous (usage_usec, monotonic) per cgroup so a second collection derives it.
	stat = _read("cpu.stat")
	if stat:
		usage_usec = None
		for line in stat.splitlines():
			if line.startswith("usage_usec"):
				usage_usec = _int(line.split()[1])
		if usage_usec is not None:
			now = time.monotonic()
			prev = _CGROUP_CPU_PREV.get(cgroup)
			if prev:
				dusage = usage_usec - prev[0]
				dt = now - prev[1]
				if dt > 0 and dusage >= 0:
					out["cpu_pct"] = round(100.0 * (dusage / 1e6) / dt, 1)
			_CGROUP_CPU_PREV[cgroup] = (usage_usec, now)
	return out


def _cpu_cap_cores(cpu_max: str | None) -> float | None:
	"""cgroup `cpu.max` ("<quota> <period>" in µs, or "max <period>") → the CPU
	ceiling in cores. quota/period is the fraction of ONE core the VM may burn;
	'max' (unlimited) and unparseable shapes yield None (no false ceiling)."""
	if not cpu_max:
		return None
	parts = cpu_max.split()
	if not parts or parts[0] == "max":
		return None
	quota = _int(parts[0])
	period = _int(parts[1]) if len(parts) > 1 else 100000
	if quota is None or not period:
		return None
	return round(quota / period, 2)


def _mem_cap_mib(memory_max: str | None) -> int | None:
	"""cgroup `memory.max` (bytes, or "max") → the memory ceiling in MiB. 'max'
	(unlimited) and non-numeric shapes yield None."""
	if not memory_max or not memory_max.isdigit():
		return None
	return int(memory_max) // (1024 * 1024)


def _vm_size(jail_root: Path) -> tuple[int | None, int | None]:
	"""(vcpus, mem_mib) from the VM's firecracker.json machine-config (spec/06).
	The single most operator-relevant fact the old page omitted."""
	cfg = read_json(jail_root / "firecracker.json")
	machine = cfg.get("machine-config") or cfg.get("machine_config") or {}
	vcpus = machine.get("vcpu_count")
	mem = machine.get("mem_size_mib")
	return (vcpus if isinstance(vcpus, int) else None, mem if isinstance(mem, int) else None)


def _lvs_disk_info() -> dict:
	"""One batched `lvs` read → {lv_name: {origin, data_percent, size_bytes,
	used_bytes}}. Joining origin + fill + size per VM one LV at a time is O(N)
	commands; this is one. (disk lineage warns lvs over hundreds of LVs still has
	a cost — one call caps it.) Sizes come in raw bytes (`--units b`); used_bytes
	is size * data_percent (the thin LV's real written extents)."""
	rows = _lvs_rows("lv_name,origin,data_percent,lv_size")
	info = {}
	for parts in rows:
		if len(parts) < 4:
			continue
		name, origin, data, size = parts[:4]
		data_percent = _float(data)
		size_bytes = _int(size)
		used_bytes = (
			int(size_bytes * data_percent / 100.0)
			if size_bytes is not None and data_percent is not None
			else None
		)
		info[name] = {
			"origin": origin or None,
			"data_percent": data_percent,
			"size_bytes": size_bytes,
			"used_bytes": used_bytes,
		}
	return info


def _migrating_uuids() -> set[str]:
	"""UUIDs of VMs with a live migration forwarder — they're mid-migration. The
	forwarder unit names carry a short id, so we cross-reference the per-VM units
	instead: a VM whose disk snapshot is a `disk-migrate` kind, or whose entry has
	a `migrating` marker file, is in flight. Best-effort: marker file wins."""
	base = ATLAS_ROOT / "virtual-machines"
	out = set()
	if not base.is_dir():
		return out
	for entry in base.iterdir():
		if entry.is_dir() and (entry / "migrating").exists():
			out.add(entry.name)
	return out


def _vm_state(uuid: str) -> str:
	"""Map the systemd unit's ActiveState to the operator's vocabulary."""
	active = run("systemctl", "is-active", f"firecracker-vm@{uuid}.service").strip()
	return {"active": "Running", "inactive": "Stopped", "failed": "Failed"}.get(active, "Stopped")


def _vm_image(jail_root: Path) -> str | None:
	"""The VM's image name is not recoverable from disk: the jail's `vmlinux` is
	a hard link into images/<name>/ with no back-reference, and no sidecar records
	the image. Return None rather than the misleading link name — the Disk column
	then shows just the data/snapshot flags. (The controller's Frappe DB is the
	real source of a VM's image; this dashboard reports only what disk proves.)"""
	return None


def _mac_from_uuid(uuid: str) -> str:
	"""06:00: + first 4 bytes of the UUID (spec/06 § MAC)."""
	try:
		import uuid as _uuidmod

		b = _uuidmod.UUID(uuid).bytes[:4]
		return "06:00:" + ":".join(f"{x:02x}" for x in b)
	except (ValueError, ImportError):
		return ""


def images() -> list[dict]:
	base = ATLAS_ROOT / "images"
	rows = []
	if not base.is_dir():
		return rows
	lv_info = _lvs_disk_info()  # base LV byte sizes, from the one batched lvs read
	for entry in sorted(base.iterdir()):
		if not entry.is_dir():
			continue
		kernel = next((f.name for f in entry.glob("vmlinux*")), None)
		rootfs = next((f.name for f in entry.glob("*.ext4")), None)
		base_lv = f"atlas-image-{entry.name}"
		rows.append(
			{
				"name": entry.name,
				"kernel": kernel,
				"rootfs": rootfs,
				"rootfs_size": _size(entry / rootfs) if rootfs else None,
				"base_lv": base_lv,
				"base_lv_size": lv_info.get(base_lv, {}).get("size_bytes"),
			}
		)
	return rows


def snapshots() -> list[dict]:
	"""Warm-golden dirs under snapshots/, plus disk snapshots read from lvs."""
	rows = []
	base = ATLAS_ROOT / "snapshots"
	if base.is_dir():
		for entry in sorted(base.iterdir()):
			if not entry.is_dir():
				continue
			sig = read_json(entry / "host-signature.json")
			rows.append(
				{
					"uuid": entry.name,
					"kind": "warm-golden",
					"vmstate_size": _size(entry / "vmstate.bin"),
					"mem_size": _size(entry / "mem.bin"),
					"captured_firecracker": sig.get("firecracker"),
					"captured_kernel": sig.get("kernel"),
				}
			)
	# Disk snapshots are LVs (atlas-snap-<uuid>), not files — read them from lvs,
	# with their fill (data_percent) from the one batched disk-info read.
	lv_info = _lvs_disk_info()
	for lv in _lvs_names():
		if lv.startswith("atlas-snap-"):
			uuid = lv[len("atlas-snap-") :]
			rows.append(
				{
					"uuid": uuid,
					"kind": "disk",
					"snapshot_lv": lv,
					"origin_lv": f"atlas-vm-{uuid}",
					"data_percent": lv_info.get(lv, {}).get("data_percent"),
				}
			)
	return rows


# --------------------------------------------------------------------------- #
# Live host state: ip / nft / lvs / systemctl
# --------------------------------------------------------------------------- #


def pool() -> dict | None:
	"""Thin-pool usage from `lvs`. data_percent / metadata_percent are the guard
	the host watches (spec/07 § out of space). Kept for the current UI's Pool bar;
	the full multi-layer view lives in `storage()`.

	We DON'T use `--select lv_attr=~t` (its match semantics vary across lvm2
	versions, and a miss silently yields None — which is one way the Pool bar ends
	up with no denominator). Instead we read every LV's attr and pick the thin
	pool ourselves: a thin-pool LV's attr starts with `t`."""
	out = run(
		"lvs",
		"--noheadings",
		"--separator",
		"|",
		"-o",
		"lv_name,vg_name,lv_size,data_percent,metadata_percent,lv_attr",
	)
	for line in out.splitlines():
		parts = [p.strip() for p in line.split("|")]
		if len(parts) < 6:
			continue
		name, vg, size, data, meta, attr = parts[:6]
		if not attr or attr[0] != "t":  # thin pools only
			continue
		return {
			"vg": vg,
			"pool": name,
			"backing": _pool_backing(),
			"backing_device": _pool_backing_device(),
			# lvs prefixes a rounded size with '<' ("<886.24g"); strip it so the
			# size string is a clean number the UI can parse.
			"size": size.lstrip("<~"),
			"data_percent": _float(data),
			"metadata_percent": _float(meta),
		}
	return None


def storage() -> dict:
	"""The full LVM stack the host's disk is carved into, layer by layer — the
	thing the old single 'Pool' line flattened away (spec/07 § storage). Four
	layers, each a real inventory with exact byte sizes so the UI can show usage
	at every level, not just the thin pool's fill:

	  · pvs         — physical volumes (the raw block devices LVM owns)
	  · vgs         — volume groups (the pool of PV space)
	  · thin_pools  — thin pools carved from a VG (data + metadata fill)
	  · volumes     — every logical volume (image / vm-disk / snapshot / pool)

	Best-effort: a box without lvm2 yields empty lists, never an error. Sizes are
	raw bytes (`--units b --nosuffix`) so the UI formats them once, consistently;
	percentages come straight from lvs."""
	return {
		"pvs": _pvs(),
		"vgs": _vgs(),
		"thin_pools": _thin_pools(),
		"volumes": volumes(),
	}


def _lvs_rows(fields: str) -> list[list[str]]:
	"""Run `lvs -o <fields>` with byte units and split into cell lists."""
	out = run("lvs", "--noheadings", "--separator", "|", "--units", "b", "--nosuffix", "-o", fields)
	return _split_report(out)


def _split_report(out: str) -> list[list[str]]:
	return [[c.strip() for c in line.split("|")] for line in out.splitlines() if line.strip()]


def _pvs() -> list[dict]:
	"""Physical volumes: the block devices under LVM, with total/free/used bytes."""
	out = run(
		"pvs",
		"--noheadings",
		"--separator",
		"|",
		"--units",
		"b",
		"--nosuffix",
		"-o",
		"pv_name,vg_name,pv_size,pv_free",
	)
	rows = []
	for parts in _split_report(out):
		if len(parts) < 4:
			continue
		name, vg, size, free = parts[:4]
		size_b, free_b = _int(size), _int(free)
		used_b = size_b - free_b if size_b is not None and free_b is not None else None
		rows.append(
			{"name": name, "vg": vg or None, "size_bytes": size_b, "free_bytes": free_b, "used_bytes": used_b}
		)
	return rows


def _vgs() -> list[dict]:
	"""Volume groups: the aggregate PV space, with total/free bytes and PV/LV counts."""
	out = run(
		"vgs",
		"--noheadings",
		"--separator",
		"|",
		"--units",
		"b",
		"--nosuffix",
		"-o",
		"vg_name,vg_size,vg_free,pv_count,lv_count",
	)
	rows = []
	for parts in _split_report(out):
		if len(parts) < 5:
			continue
		name, size, free, pvc, lvc = parts[:5]
		size_b, free_b = _int(size), _int(free)
		used_b = size_b - free_b if size_b is not None and free_b is not None else None
		rows.append(
			{
				"name": name,
				"size_bytes": size_b,
				"free_bytes": free_b,
				"used_bytes": used_b,
				"pv_count": _int(pvc),
				"lv_count": _int(lvc),
			}
		)
	return rows


def _thin_pools() -> list[dict]:
	"""Every thin pool (a VG may carve more than one): size + data/metadata fill.
	This is where the Pool bar's fill comes from — data_percent is the real number,
	so it reads the host's true occupancy (e.g. 4.6%), not a pinned 100%."""
	rows = []
	for parts in _lvs_rows("lv_name,vg_name,lv_size,data_percent,metadata_percent,lv_attr"):
		if len(parts) < 6:
			continue
		name, vg, size, data, meta, attr = parts[:6]
		if not attr or attr[0] != "t":
			continue
		rows.append(
			{
				"name": name,
				"vg": vg or None,
				"size_bytes": _int(size),
				"backing": _pool_backing(),
				"data_percent": _float(data),
				"metadata_percent": _float(meta),
			}
		)
	return rows


def _pool_backing() -> str:
	devices = ATLAS_ROOT / "pool" / "pool-devices"
	if devices.exists():
		body = devices.read_text().strip()
		return "loopback" if "atlas-pool.img" in body else "device"
	return "loopback" if (ATLAS_ROOT / "pool" / "atlas-pool.img").exists() else ""


def _pool_backing_device() -> str | None:
	"""The actual block device backing the pool (e.g. '/dev/md2'), from the first
	line of pool-devices. Self-documents the pool row: 'device' + which device.
	None when the marker is absent (loopback hosts / no pool)."""
	devices = ATLAS_ROOT / "pool" / "pool-devices"
	try:
		for line in devices.read_text().splitlines():
			token = line.strip()
			if token and not token.startswith("#"):
				return token
	except OSError:
		pass
	return None


def _lvs_names() -> list[str]:
	out = run("lvs", "--noheadings", "-o", "lv_name")
	return [line.strip() for line in out.splitlines() if line.strip()]


def addresses() -> list[dict]:
	"""`ip -j addr` flattened to one row per address."""
	rows = []
	for iface in _ip_json("addr"):
		name = iface.get("ifname", "")
		for info in iface.get("addr_info", []):
			rows.append(
				{
					"interface": name,
					"family": info.get("family"),
					"address": f"{info.get('local')}/{info.get('prefixlen')}",
					"scope": info.get("scope"),
				}
			)
	return rows


def interfaces() -> list[dict]:
	rows = []
	for iface in _ip_json("link"):
		name = iface.get("ifname", "")
		rows.append(
			{
				"name": name,
				"mac": iface.get("address"),
				"mtu": iface.get("mtu"),
				"state": iface.get("operstate", ""),
				"kind": iface.get("link_type", "device"),
			}
		)
	return rows


def routes() -> list[dict]:
	rows = []
	for family, flag in (("inet", "-4"), ("inet6", "-6")):
		for route in _ip_json("route", flag):
			rows.append(
				{
					"family": family,
					"dest": route.get("dst"),
					"via": route.get("gateway"),
					"dev": route.get("dev"),
					"table": route.get("table", "main"),
				}
			)
	return rows


def neigh_proxy() -> list[dict]:
	"""Proxy-NDP entries on the uplink — the trick that makes each VM's /128
	reachable (spec/06)."""
	rows = []
	out = run("ip", "-6", "neigh", "show", "proxy")
	for line in out.splitlines():
		tokens = line.split()
		if "dev" in tokens:
			rows.append({"address": tokens[0], "dev": tokens[tokens.index("dev") + 1]})
	return rows


def ip_rules() -> list[dict]:
	"""Policy routing rules (the Reserved-IP egress rules live here)."""
	rows = []
	for rule in _ip_json("rule"):
		src = rule.get("src")
		if not src or src == "all":
			continue
		rows.append(
			{
				"priority": rule.get("priority"),
				"from": src,
				"table": rule.get("table"),
			}
		)
	return rows


def reserved_ips() -> list[dict]:
	"""Reserved IPv4s in play — derived from the VMs that carry a RESERVED_IPV4
	in their network.env (the on-disk source of truth, spec/06)."""
	rows = []
	for vm in virtual_machines():
		if vm.get("reserved_ipv4"):
			rows.append(
				{
					"address": vm["reserved_ipv4"],
					"attached_vm": vm["uuid"],
					"guest_ipv4": vm.get("ipv4_guest"),  # the DNAT target — self-documents the row
					"anchor": None,
					"anchor_gateway": None,
				}
			)
	return rows


def nft_tables() -> list[dict]:
	"""`nft -j list ruleset` reshaped to family/name/chains/rules. We flatten each
	rule back to its text form so the page shows exactly what `nft list` shows."""
	raw = run("nft", "-j", "list", "ruleset")
	try:
		doc = json.loads(raw).get("nftables", [])
	except (ValueError, AttributeError):
		return []

	tables = {}
	order = []
	for item in doc:
		if "table" in item:
			t = item["table"]
			key = (t["family"], t["name"])
			tables[key] = {
				"family": t["family"],
				"name": t["name"],
				"persisted": t["name"] != "atlas",  # data-plane table is ephemeral
				"chains": {},
				"chain_order": [],
			}
			order.append(key)
	for item in doc:
		if "chain" in item:
			c = item["chain"]
			key = (c["family"], c["table"])
			if key in tables:
				tables[key]["chains"][c["name"]] = {
					"name": c["name"],
					"type": c.get("type", ""),
					"rules": [],
				}
				tables[key]["chain_order"].append(c["name"])
	for item in doc:
		if "rule" in item:
			r = item["rule"]
			key = (r["family"], r["table"])
			chain = tables.get(key, {}).get("chains", {}).get(r["chain"])
			if chain is not None:
				chain["rules"].append(_nft_rule_text(r))

	result = []
	for key in order:
		t = tables[key]
		result.append(
			{
				"family": t["family"],
				"name": t["name"],
				"persisted": t["persisted"],
				"chains": [t["chains"][n] for n in t["chain_order"]],
			}
		)
	return result


def _nft_rule_text(rule: dict) -> str:
	"""Render one nft JSON rule back to its textual form — the syntax `nft list
	ruleset` prints, e.g. `ip daddr 51.159.76.127 dnat ip to 100.64.0.14`.

	`nft -j list ruleset` does NOT carry the pretty text; it carries a structured
	`expr` list (one entry per statement). We walk that tree and re-emit the human
	form, so the Firewall section reads like `nft list` instead of a JSON blob.
	Best-effort: an expr shape we don't model degrades to a compact dump of just
	that fragment, never the whole rule — so a novel construct is still legible."""
	parts = [frag for expr in rule.get("expr", []) if (frag := _nft_expr_text(expr))]
	return " ".join(parts)


def _nft_expr_text(expr: dict) -> str:
	"""Render one nft expression node. nft groups a rule's statements into a list;
	each is a dict with a single key naming the statement kind (`match`, `dnat`,
	`snat`, `masquerade`, `accept`, `drop`, `counter`, …)."""
	if not isinstance(expr, dict):
		return ""
	if "match" in expr:
		return _nft_match_text(expr["match"])
	# Terminal verdicts / statements carried as a bare key.
	for verb in ("accept", "drop", "reject", "return", "masquerade"):
		if verb in expr:
			return verb
	# NAT with a target address: {"dnat": {"addr": "100.64.0.14", "family": "ip"}}.
	for verb in ("dnat", "snat", "redirect"):
		if verb in expr:
			target = expr[verb]
			if isinstance(target, dict):
				fam = target.get("family")
				addr = target.get("addr")
				port = target.get("port")
				to = addr if addr else ""
				if port:
					to = f"{to}:{port}" if to else str(port)
				return (
					f"{verb} {fam} to {to}".replace("  ", " ").strip() if fam else f"{verb} to {to}".strip()
				)
			return verb
	if "counter" in expr:
		return "counter"
	if "jump" in expr and isinstance(expr["jump"], dict):
		return f"jump {expr['jump'].get('target', '')}".strip()
	if "goto" in expr and isinstance(expr["goto"], dict):
		return f"goto {expr['goto'].get('target', '')}".strip()
	# Unmodelled node: dump just this fragment so the rest of the rule stays clean.
	return json.dumps(expr, separators=(",", ":"))


def _nft_match_text(match: dict) -> str:
	"""Render a `match` node: {op, left, right}. `left` is usually a payload
	({payload:{protocol,field}}) or a meta ({meta:{key}}); `right` the value.
	Covers the ip/ip6 daddr/saddr, iifname/oifname, and set/prefix forms Atlas
	emits — the ones the Firewall page needs to read like `nft list`."""
	left = _nft_operand_text(match.get("left"))
	right = _nft_operand_text(match.get("right"))
	op = match.get("op", "==")
	# nft prints `==` implicitly (`ip daddr X`), and other ops explicitly.
	if op in ("==", "in"):
		return f"{left} {right}".strip()
	return f"{left} {op} {right}".strip()


def _nft_operand_text(operand) -> str:
	"""Render one side of a match. Payload → `ip daddr`; meta → `iifname`; a raw
	scalar → itself; a prefix → `addr/len`; a set → `{ a, b }`."""
	if operand is None:
		return ""
	if isinstance(operand, (str, int)):
		return str(operand)
	if isinstance(operand, dict):
		if "payload" in operand:
			p = operand["payload"]
			proto = p.get("protocol", "")
			field = p.get("field", "")
			# nft prints daddr/saddr under the header proto: `ip daddr`, `ip6 saddr`.
			return f"{proto} {field}".strip()
		if "meta" in operand:
			return operand["meta"].get("key", "")
		if "prefix" in operand:
			pre = operand["prefix"]
			return f"{_nft_operand_text(pre.get('addr'))}/{pre.get('len')}"
		if "set" in operand:
			items = operand["set"]
			rendered = (
				", ".join(_nft_operand_text(i) for i in items) if isinstance(items, list) else str(items)
			)
			return "{ " + rendered + " }"
		# Quote a bare interface-name value the way nft does.
	if isinstance(operand, str):
		return operand
	return json.dumps(operand, separators=(",", ":"))


def units() -> list[dict]:
	"""Atlas-relevant systemd units: the per-VM firecracker units, the pool
	oneshot, migration forwarders, and the persisted firewall."""
	out = run(
		"systemctl",
		"list-units",
		"--all",
		"--no-legend",
		"--plain",
		"--type=service",
	)
	rows = []
	for line in out.splitlines():
		tokens = line.split()
		if len(tokens) < 4:
			continue
		name, load, active, sub = tokens[0], tokens[1], tokens[2], tokens[3]
		kind = _unit_kind(name)
		if kind is None:
			continue
		# The remaining tokens are the unit's free-text DESCRIPTION column. The
		# forwarder units carry their socat command line here, which names the
		# migration peer host:port — migrations() reads it back out.
		description = " ".join(tokens[4:])
		rows.append(
			{
				"name": name,
				"load": load,
				"active": active,
				"sub": sub,
				"kind": kind,
				"description": description,
			}
		)
	return rows


def _unit_kind(name: str) -> str | None:
	if name.startswith("firecracker-vm@"):
		return "vm"
	if name.startswith("atlas-migrate-forward@"):
		return "migration-forwarder"
	if name == "atlas-pool.service":
		return "pool"
	if name == "nftables.service":
		return "firewall"
	return None


# --------------------------------------------------------------------------- #
# Storage: the LVM domain lifted out of Images (Plan A). Pool lives above; this
# is the volume inventory (every LV, its role and fill) + the disk snapshots.
# --------------------------------------------------------------------------- #


def volumes() -> list[dict]:
	"""Every logical volume, one batched `lvs` read, classified by naming scheme
	(spec/07): base image LVs, per-VM disks, disk snapshots, the thin pool. This
	is the Storage domain's main table — the host's disk carved into volumes."""
	out = run(
		"lvs",
		"--noheadings",
		"--separator",
		"|",
		"--units",
		"b",
		"--nosuffix",
		"-o",
		"lv_name,vg_name,lv_size,origin,data_percent,lv_attr",
	)
	rows = []
	for line in out.splitlines():
		parts = [p.strip() for p in line.split("|")]
		if len(parts) < 6:
			continue
		name, vg, size, origin, data, attr = parts[:6]
		size_b = _int(size)
		rows.append(
			{
				"name": name,
				"vg": vg,
				# `size` stays human (k/m/g) for the current UI; `size_bytes` is the
				# raw count the storage hierarchy formats itself.
				"size": _human_bytes(size_b) if size_b is not None else size,
				"size_bytes": size_b,
				"role": _lv_role(name, attr),
				"origin": origin or None,
				"data_percent": _float(data),
			}
		)
	return rows


def _lv_role(name: str, attr: str) -> str:
	if attr and attr[0] == "t":
		return "pool"
	if name.startswith("atlas-image-"):
		return "image"
	if name.startswith("atlas-snap-"):
		return "snapshot"
	if name.startswith("atlas-vm-"):
		return "vm-disk"
	return "other"


# --------------------------------------------------------------------------- #
# Network extensions (Plan A): proxy maps, the private host-mesh, migrations.
# --------------------------------------------------------------------------- #


# The proxy's live map is a lua_shared_dict INSIDE the proxy guest, reachable only
# over the guest admin unix socket (spec/12 § the live map / admin socket). The old
# `proxy/map.json` this used to read was a stale on-disk guess that never populated
# on real hosts — the proxy doesn't dump to the HOST, it serves from the GUEST. So
# we read it the way the controller's reconciler does: SSH into the proxy guest and
# ask nginx's admin socket for its live `sites` (http) + SNI (stream) maps.
_PROXY_ADMIN_SOCKET = "/run/nginx/admin.sock"
# Which VM is the proxy is NOT recoverable from host disk (`is_proxy` lives only in
# the controller's Frappe DB), so the operator names it — the guest ssh dest, e.g.
# `root@100.64.0.2` — via the environment. `setup.sh`/the proxy can wire this.
_PROXY_GUEST = os.environ.get("ATLAS_PROXY_GUEST", "")
_PROXY_SSH_KEY = os.environ.get("ATLAS_PROXY_GUEST_SSH_KEY", "")


def proxy_maps() -> list[dict]:
	"""The routes the reverse proxy is LIVE-publishing (spec/12, spec/17). Each row
	is one published route: `sites` = a subdomain hostname → a VM's guest `/128`
	(http/443), `sni` = a custom domain → backend (stream/443 SNI passthrough).

	Read live from the proxy guest's nginx admin socket over SSH — the same read
	the controller's `read_live_maps` makes — so the page shows what the proxy
	ACTUALLY serves, not a stale on-disk mirror. Best-effort: no proxy guest
	configured / unreachable → empty list, never an error (a host that runs no
	proxy simply has none)."""
	dest = _PROXY_GUEST
	if not dest:
		return []
	rows = []
	rows += _proxy_http_rows(dest)
	rows += _proxy_sni_rows(dest)
	return rows


def _proxy_guest_ssh(dest: str, remote_cmd: str) -> str:
	"""Run one read-only command inside the proxy guest over SSH, return stdout or
	'' on any failure. Mirrors the reconciler's guest-SSH path (BatchMode, the
	Atlas guest key when provided)."""
	argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
	if _PROXY_SSH_KEY:
		argv += ["-o", f"IdentityFile={_PROXY_SSH_KEY}", "-o", "IdentitiesOnly=yes"]
	argv += [dest, remote_cmd]
	return run(*argv)


def _proxy_http_rows(dest: str) -> list[dict]:
	"""The http `sites` map: `GET /map` off the admin socket → {hostname: backend}.
	Each entry is a subdomain published to a VM's guest `/128` on :443."""
	raw = _proxy_guest_ssh(dest, f"curl -s --unix-socket {_PROXY_ADMIN_SOCKET} http://admin/map")
	try:
		doc = json.loads(raw) if raw else {}
	except ValueError:
		return []
	rows = []
	for host, backend in doc.items() if isinstance(doc, dict) else []:
		rows.append(
			{
				"listen": ":443",
				"protocol": "https",
				"sni": host,
				"backend": backend if isinstance(backend, str) else json.dumps(backend),
				"vm": None,  # backend is a guest /128; the UI joins it to a VM by ipv6
			}
		)
	return rows


def _proxy_sni_rows(dest: str) -> list[dict]:
	"""The stream SNI map: `stream-admin GET-SNI` → {domain: backend}. Each entry is
	a custom domain passed through on :443 by SNI to a backend (spec/17)."""
	raw = _proxy_guest_ssh(dest, "stream-admin GET-SNI")
	try:
		doc = json.loads(raw) if raw else {}
	except ValueError:
		return []
	rows = []
	for domain, backend in doc.items() if isinstance(doc, dict) else []:
		rows.append(
			{
				"listen": ":443",
				"protocol": "sni-passthrough",
				"sni": domain,
				"backend": backend if isinstance(backend, str) else json.dumps(backend),
				"vm": None,
			}
		)
	return rows


def private_mesh() -> list[dict]:
	"""The WireGuard host-mesh private network (idea/private, Phase-1). Each VM on
	the mesh has a private /128 route into `fdaa::/16` plus isolation rules. Rows
	are the per-VM private endpoints read from network.env sidecars; the mesh
	device + peers come from `wg show` when the host runs the mesh."""
	rows = []
	base = ATLAS_ROOT / "virtual-machines"
	if base.is_dir():
		for entry in sorted(base.iterdir()):
			if not entry.is_dir():
				continue
			env = read_env(entry / "network.env")
			private = env.get("PRIVATE_IPV6")
			if private:
				rows.append(
					{
						"address": _strip_cidr(private),
						"attached_vm": entry.name,
						"device": env.get("PRIVATE_DEVICE", "atlas-mesh"),
					}
				)
	return rows


def migrations() -> list[dict]:
	"""VMs mid-migration: one row per live migration forwarder. The socat unit's
	description carries the peer host:port, so we read it there —
	the forwarder is the observable proof a migration is in flight.

	units() emits each forwarder's `description`, so `peer` resolves from it."""
	rows = []
	for u in units():
		if u.get("kind") != "migration-forwarder":
			continue
		rows.append(
			{
				"unit": u["name"],
				"state": u.get("sub"),
				"peer": _forwarder_peer(u.get("description", "")),
			}
		)
	return rows


def _forwarder_peer(description: str) -> str | None:
	"""Pull the TCP peer out of a socat forwarder description, e.g.
	'socat TUN mig6-… <-> TCP:51.159.110.51:19657' → '51.159.110.51:19657'."""
	match = re.search(r"TCP:(\S+)", description)
	return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# System (Plan A, renamed from the old System): host tasks, disks, users,
# processes — the host primitives, alongside Units and Packages.
# --------------------------------------------------------------------------- #


def tasks() -> list[dict]:
	"""Atlas host-side tasks (spec/task). Only meaningful once task-tracking lives
	host-side; read from the task journal on disk when present, else empty."""
	rows = []
	doc = read_json(ATLAS_ROOT / "tasks" / "recent.json")
	for t in doc.get("tasks", []) if isinstance(doc, dict) else []:
		rows.append(
			{
				"id": t.get("id"),
				"name": t.get("name"),
				"status": t.get("status"),
				"started_at": t.get("started_at"),
				"duration": t.get("duration"),
			}
		)
	return rows


def disks() -> list[dict]:
	"""Block devices from `lsblk` + filesystem usage from `df` for the mounts that
	matter. The physical backing under the pool + the root fs — 'is this host
	running out of real disk', below the thin-pool abstraction."""
	rows = []
	raw = run("lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MOUNTPOINT,MODEL,ROTA")
	try:
		tree = json.loads(raw).get("blockdevices", []) if raw else []
	except (ValueError, AttributeError):
		tree = []

	def walk(node):
		for dev in node:
			rows.append(
				{
					"name": dev.get("name"),
					"kind": dev.get("type"),
					"size": _human_bytes(dev.get("size")),
					"mount": dev.get("mountpoint"),
					"model": (dev.get("model") or "").strip() or None,
					"rota": dev.get("rota"),
				}
			)
			if dev.get("children"):
				walk(dev["children"])

	walk(tree)
	return rows


def users() -> list[dict]:
	"""Human + service accounts that can touch the host: login-capable users from
	/etc/passwd plus who holds sudo (spec/host). The jailer runs each VM as a
	distinct uid; those are surfaced per-VM, not here."""
	rows = []
	sudoers = _sudo_users()
	try:
		for line in Path("/etc/passwd").read_text().splitlines():
			parts = line.split(":")
			if len(parts) < 7:
				continue
			name, _, uid, _, _, home, shell = parts[:7]
			# Login-capable accounts only (real shell), plus root.
			if shell.endswith(("nologin", "false")) and name != "root":
				continue
			rows.append(
				{
					"name": name,
					"uid": _int(uid),
					"shell": shell,
					"home": home,
					"sudo": name in sudoers,
				}
			)
	except OSError:
		pass
	return rows


def _sudo_users() -> set[str]:
	"""Members of the sudo/wheel group — best-effort from /etc/group."""
	out = set()
	try:
		for line in Path("/etc/group").read_text().splitlines():
			parts = line.split(":")
			if len(parts) >= 4 and parts[0] in ("sudo", "wheel"):
				out.update(m for m in parts[3].split(",") if m)
	except OSError:
		pass
	return out


def processes() -> list[dict]:
	"""The Atlas process tree: firecracker + jailer processes, one row each, with
	the VM uuid they serve (jailer's chroot path carries it). Maps a stray process
	back to its machine (via the jailer uid). Best-effort via `ps`."""
	rows = []
	out = run("ps", "-eo", "pid,user,rss,args", "--no-headers")
	for line in out.splitlines():
		parts = line.split(None, 3)
		if len(parts) < 4:
			continue
		pid, user, rss, args = parts
		if "firecracker" not in args and "jailer" not in args:
			continue
		m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", args)
		rows.append(
			{
				"pid": _int(pid),
				"user": user,
				"rss": _human_bytes((_int(rss) or 0) * 1024),
				"kind": "jailer" if "jailer" in args else "firecracker",
				"vm": m.group(1) if m else None,
			}
		)
	return rows


def _human_bytes(n) -> str | None:
	"""Human size of a raw byte count (k/m/g/t), or None."""
	n = _int(n) if isinstance(n, str) else n
	if not isinstance(n, int):
		return None
	value = float(n)
	for unit in ("", "k", "m", "g", "t"):
		if value < 1024 or unit == "t":
			return f"{value:.0f}{unit}" if unit else f"{int(value)}"
		value /= 1024.0


# --------------------------------------------------------------------------- #
# Metrics: a live host sampler (spec/24-metrics.md § Host level). This is NOT the
# full metrics.db subsystem (SQLite store + rollups) — that lives on set-up hosts
# and is out of scope for a read-only `ls`/`cat`. This is a stdlib-only, zero-
# install sampler that reads the SAME /proc + lvs sources spec/24 names and emits
# the `metrics.series` shape the charts already consume ({key: {unit, points[]}}).
#
# One HTTP request is a point-in-time snapshot, so we keep a small in-memory ring
# (per server process) and append one sample per /api/state read: the page is
# refresh-to-update, so successive refreshes accumulate REAL history rather than
# fabricating a window. Counter series (cpu / net / disk) need two readings to
# yield a rate — the first sample seeds the counters and contributes no point, so
# the series never shows a fake spike from a cold start (spec/24 § counters are
# monotonic; rate is computed from adjacent samples).
# --------------------------------------------------------------------------- #

# {key: unit} — the six series the Analytics/Overview charts render, in the mock
# contract's vocabulary (see mock/state.json § metrics.series).
_METRIC_UNITS = {
	"cpu_util_pct": "%",
	"mem_used_mib": "MiB",
	"disk_io_iops": "iops",
	"net_rx_mbps": "Mb/s",
	"net_tx_mbps": "Mb/s",
	"pool_used_pct": "%",
}
_METRIC_KEYS = list(_METRIC_UNITS)
_METRIC_WINDOW = 48  # points retained per series — matches the mock fixtures.

# Raw counters + timestamp from the previous sample, so rate series can diff.
_METRIC_PREV: dict = {}
# {key: [point, ...]} accumulated across requests, newest last, capped at window.
_METRIC_HISTORY: dict = {key: [] for key in _METRIC_KEYS}


def metrics() -> dict:
	"""Sample the host once, fold the reading into the in-memory history, and
	return the accumulated series. Best-effort throughout: an unreadable /proc
	source drops that one series' point (None), never the whole document."""
	now = time.monotonic()
	gauges = _metric_gauges()  # instantaneous readings (mem, pool)
	rates = _metric_rates(now)  # counter-derived per-second rates (cpu, net, disk)
	sample = {**gauges, **rates}

	# Seed request (no prior counters): gauges are already valid, but the rate
	# series are still None — skip appending so the first point isn't a null hole.
	seeding = rates.get("_seeding", False)
	rates.pop("_seeding", None)

	for key in _METRIC_KEYS:
		value = sample.get(key)
		if value is None and key in _METRIC_HISTORY and not _METRIC_HISTORY[key] and seeding:
			# Nothing to plot yet for this series on the seed request.
			continue
		if value is None:
			continue
		history = _METRIC_HISTORY[key]
		history.append(round(value, 2))
		del history[:-_METRIC_WINDOW]

	series = {
		key: {"unit": _METRIC_UNITS[key], "points": list(points)}
		for key, points in _METRIC_HISTORY.items()
		if points
	}
	return {
		"collected_at": datetime.datetime.now(datetime.UTC)
		.replace(microsecond=0)
		.isoformat()
		.replace("+00:00", "Z"),
		"series": series,
	}


def _metric_gauges() -> dict:
	"""Instantaneous readings that need no delta: memory used + pool fill."""
	out = {}
	total = _mem_total_mib()
	avail = _mem_available_mib()
	if total is not None and avail is not None:
		out["mem_used_mib"] = max(total - avail, 0)
	p = pool()
	if p and p.get("data_percent") is not None:
		out["pool_used_pct"] = p["data_percent"]
	return out


def _metric_rates(now: float) -> dict:
	"""CPU / network / disk rates from the delta against the previous sample.

	Reads the raw counters now, diffs them against `_METRIC_PREV`, and stashes the
	fresh counters for next time. On the very first call there is no prior sample,
	so every rate is None and `_seeding` marks the caller to skip the empty point."""
	cpu = _cpu_jiffies()  # (busy, total)
	net = _net_counters()  # (rx_bytes, tx_bytes) on the uplink
	dsk = _disk_ios()  # total read+write ios across real block devices

	prev = _METRIC_PREV
	out: dict = {"cpu_util_pct": None, "net_rx_mbps": None, "net_tx_mbps": None, "disk_io_iops": None}
	if not prev:
		out["_seeding"] = True
	else:
		dt = now - prev.get("t", now)
		if dt > 0:
			if cpu and prev.get("cpu"):
				busy = cpu[0] - prev["cpu"][0]
				total = cpu[1] - prev["cpu"][1]
				if total > 0:
					out["cpu_util_pct"] = max(0.0, min(100.0, 100.0 * busy / total))
			if net and prev.get("net"):
				out["net_rx_mbps"] = max(0.0, (net[0] - prev["net"][0]) * 8 / 1e6 / dt)
				out["net_tx_mbps"] = max(0.0, (net[1] - prev["net"][1]) * 8 / 1e6 / dt)
			if dsk is not None and prev.get("dsk") is not None:
				out["disk_io_iops"] = max(0.0, (dsk - prev["dsk"]) / dt)

	_METRIC_PREV.clear()
	_METRIC_PREV.update(t=now, cpu=cpu, net=net, dsk=dsk)
	return out


def _mem_available_mib() -> int | None:
	"""MemAvailable from /proc/meminfo, in MiB — the honest 'free' the kernel
	computes (accounts for reclaimable cache), so used = total - available."""
	try:
		for line in Path("/proc/meminfo").read_text().splitlines():
			if line.startswith("MemAvailable:"):
				return int(line.split()[1]) // 1024
	except (OSError, ValueError, IndexError):
		pass
	return None


def _cpu_jiffies() -> tuple[int, int] | None:
	"""(busy, total) jiffies from the aggregate `cpu` line of /proc/stat. busy is
	everything but idle+iowait; utilization is the busy fraction of the delta."""
	try:
		for line in Path("/proc/stat").read_text().splitlines():
			if line.startswith("cpu "):
				fields = [int(x) for x in line.split()[1:]]
				idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
				total = sum(fields)
				return (total - idle, total)
	except (OSError, ValueError, IndexError):
		pass
	return None


def _net_counters() -> tuple[int, int] | None:
	"""(rx_bytes, tx_bytes) on the host uplink from /proc/net/dev. The uplink is
	the device carrying the default v6 route (same as host_facts.uplink)."""
	dev = _uplink()
	if not dev:
		return None
	try:
		for line in Path("/proc/net/dev").read_text().splitlines():
			name, _, rest = line.partition(":")
			if name.strip() != dev or not rest:
				continue
			cols = rest.split()
			return (int(cols[0]), int(cols[8]))  # rx bytes, tx bytes
	except (OSError, ValueError, IndexError):
		pass
	return None


def _disk_ios() -> int | None:
	"""Total completed IOs (reads + writes) across real block devices from
	/proc/diskstats. Skips partitions and dm/loop pseudo-devices so the rate is
	physical-disk IOPS, not double-counted through the LVM stack."""
	try:
		total = 0
		seen = False
		for line in Path("/proc/diskstats").read_text().splitlines():
			f = line.split()
			if len(f) < 8:
				continue
			name = f[2]
			if name[-1:].isdigit() and not name.startswith("nvme"):
				continue  # a partition (sda1); nvme0n1 legitimately ends in a digit
			if name.startswith(("dm-", "loop", "ram", "md")):
				continue
			total += int(f[3]) + int(f[7])  # reads completed + writes completed
			seen = True
		return total if seen else None
	except (OSError, ValueError, IndexError):
		return None


# --------------------------------------------------------------------------- #
# ip -j wrapper + tiny formatters
# --------------------------------------------------------------------------- #


def _ip_json(obj: str, *flags: str) -> list:
	raw = run("ip", "-j", *flags, obj, "show")
	try:
		return json.loads(raw) if raw else []
	except ValueError:
		return []


def _strip_cidr(value: str | None) -> str | None:
	return value.split("/", 1)[0] if value else None


def _size(path: Path) -> str | None:
	"""Human size (k/m/g) of a file, or None if absent — mirrors `ls -h`."""
	try:
		n = path.stat().st_size
	except OSError:
		return None
	for unit in ("", "k", "m", "g", "t"):
		if n < 1024 or unit == "t":
			return f"{n:.1f}{unit}" if unit else f"{n}"
		n /= 1024.0


def _float(value: str) -> float | None:
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _int(value: str | None) -> int | None:
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def build_state() -> dict:
	"""Assemble the one document the frontend renders. Every section is
	independent and best-effort, so a partial host still produces a full page."""
	return {
		"host": host_facts(),
		"pool": pool(),
		"virtual_machines": virtual_machines(),
		"images": images(),
		"snapshots": snapshots(),
		# Storage domain (Plan A): LVM volumes lifted out of Images. `volumes` stays
		# for the current UI; `storage` is the full layered view (pv/vg/pool/lv).
		"volumes": volumes(),
		"storage": storage(),
		"addresses": addresses(),
		"interfaces": interfaces(),
		"routes": routes(),
		"neigh_proxy": neigh_proxy(),
		"ip_rules": ip_rules(),
		"reserved_ips": reserved_ips(),
		# Network extensions (Plan A): proxy/TCP maps, the private host-mesh, and
		# in-flight migrations.
		"proxy_maps": proxy_maps(),
		"private_mesh": private_mesh(),
		"migrations": migrations(),
		"nft_tables": nft_tables(),
		"units": units(),
		# System extensions (Plan A): host primitives beside Units/Packages.
		"tasks": tasks(),
		"disks": disks(),
		"users": users(),
		"processes": processes(),
		# Live host telemetry (spec/24 § Host level), sampled per request into an
		# in-memory ring — a real, accumulating window, not the frozen mock.
		"metrics": metrics(),
	}


# --------------------------------------------------------------------------- #
# HTTP: /api/state + static dist
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
	def do_GET(self):
		if self.path.split("?")[0] == "/api/state":
			return self._serve_state()
		return self._serve_static()

	def _serve_state(self):
		body = json.dumps(build_state()).encode()
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(body)

	def _serve_static(self):
		rel = self.path.split("?")[0].lstrip("/") or "index.html"
		target = (DIST_DIR / rel).resolve()
		# Contain path traversal + fall back to index.html (SPA single route).
		if DIST_DIR.resolve() not in target.parents and target != DIST_DIR.resolve():
			target = DIST_DIR / "index.html"
		if not target.is_file():
			target = DIST_DIR / "index.html"
		if not target.is_file():
			self.send_error(404, "dist not built — run `npm run build`")
			return
		body = target.read_bytes()
		self.send_response(200)
		self.send_header("Content-Type", _content_type(target))
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, *args):  # keep the console quiet
		pass


def _content_type(path: Path) -> str:
	return {
		".html": "text/html",
		".js": "text/javascript",
		".css": "text/css",
		".json": "application/json",
		".svg": "image/svg+xml",
		".woff2": "font/woff2",
		".woff": "font/woff",
		".ico": "image/x-icon",
	}.get(path.suffix, "application/octet-stream")


def collect_once() -> dict:
	"""One full state document for the `--collect` CLI path (used by the dev proxy,
	which pushes this file over SSH and reads stdout). Identical to what /api/state
	serves, EXCEPT the metrics section: a pushed script runs once and exits, so it
	cannot diff counters into rates. Instead it emits the RAW instantaneous counters
	under metrics.raw; the long-lived proxy holds the history ring and derives the
	rate series across its own repeated polls — the same job the HTTP server does
	in-process. Gauges (mem/pool) are point-in-time, so they travel resolved."""
	state = build_state()
	total, avail = _mem_total_mib(), _mem_available_mib()
	state["metrics"] = {
		"collected_at": datetime.datetime.now(datetime.UTC)
		.replace(microsecond=0)
		.isoformat()
		.replace("+00:00", "Z"),
		"raw": {
			"mono": time.monotonic(),
			"cpu": _cpu_jiffies(),
			"net": _net_counters(),
			"disk_ios": _disk_ios(),
			"mem_used_mib": (max(total - avail, 0) if total is not None and avail is not None else None),
			"pool_used_pct": (pool() or {}).get("data_percent"),
		},
	}
	return state


def _systemd_socket() -> socket.socket | None:
	"""Return the listening socket systemd handed us via socket activation, or None.

	Under socket activation, systemd opens the listening socket itself and starts
	this service on the first connection, passing the socket as fd 3 with
	`LISTEN_FDS`/`LISTEN_PID` set (the sd_listen_fds protocol). We adopt that fd
	instead of binding our own — the service needs no port of its own and can be
	stopped when idle. Absent those vars (a plain `python3 server.py`), return None
	and fall back to binding `BIND:PORT`.
	"""
	if os.environ.get("LISTEN_PID") != str(os.getpid()):
		return None
	if int(os.environ.get("LISTEN_FDS", "0")) < 1:
		return None
	SD_LISTEN_FDS_START = 3
	return socket.socket(fileno=SD_LISTEN_FDS_START)


class _ActivatedServer(ThreadingHTTPServer):
	"""ThreadingHTTPServer that adopts a pre-bound, pre-listening socket when one
	is supplied (systemd socket activation), else binds/listens itself."""

	def __init__(self, addr, handler, activated=None):
		self._activated = activated
		super().__init__(addr, handler, bind_and_activate=activated is None)
		if activated is not None:
			self.socket = activated
			self.server_address = activated.getsockname()


def main():
	if "--collect" in sys.argv:
		# Emit one state document as JSON and exit — the dev proxy's transport.
		sys.stdout.write(json.dumps(collect_once()))
		return
	activated = _systemd_socket()
	server = _ActivatedServer((BIND, PORT), Handler, activated=activated)
	where = "systemd socket" if activated is not None else f"http://{BIND}:{PORT}"
	print(f"atlas host dashboard on {where}  (root={ATLAS_ROOT})")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		server.shutdown()


if __name__ == "__main__":
	main()
