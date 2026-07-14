"""Whitelisted helper used by the Virtual Machine creation form.

Returns "what does this Server have, and how much of it is already spoken for?"
so the operator can see oversubscription before clicking Provision. Capacity is
tracked on three axes — CPU, RAM, and pool disk — each reported as a
`{total, effective, used}` block. `effective is None` on an axis means the host
is *uncatalogued* on that axis (the agent hasn't reported a total, and for CPU
there is no slug either): the host reports unlimited capacity on that axis and
placement puts a VM there — the operator vouched for it by marking it Active.

CPU is oversubscribable: a VM's `cpu_max_cores` is a cgroup cpu.max bandwidth
cap, not a pinned core, so a host can safely back more vCPUs than it physically
has. The fleet-wide multiplier is `Atlas Settings.overprovision_factor`
(default 1 — no oversubscription until the operator raises it), applied to the
CPU axis only. RAM and disk are hard fits — a VM's memory/disk either fits or it
doesn't — so their effective budget is the raw total, no factor.

Host totals are agent-reported and stamped on `Server` (vcpus_total,
memory_megabytes_total, pool_disk_gigabytes_total). Until the agent ships them
these are unset → every axis uncatalogued → unlimited, which is exactly the
pre-three-resource behavior. For CPU there is also a legacy fallback: the static
slug→vCPU dict, so vCPU accounting keeps working on hosts catalogued the old way
before the agent reports.
"""

import math

import frappe

from atlas.atlas.sizes import SHARE_UNIT

# vCPUs per DigitalOcean size slug. Legacy fallback for the CPU axis when the
# agent hasn't stamped `vcpus_total`. Hand-maintained; a missing slug (and no
# agent total) reports unlimited CPU from `capacity_for_server` and the client
# falls back to a "—" total.
DIGITALOCEAN_VCPUS_BY_SIZE: dict[str, int] = {
	"s-1vcpu-1gb": 1,
	"s-1vcpu-2gb": 1,
	"s-2vcpu-2gb": 2,
	"s-2vcpu-4gb-intel": 2,
	"s-2vcpu-4gb": 2,
	"s-4vcpu-8gb": 4,
	"s-8vcpu-16gb-intel": 8,
	"s-8vcpu-16gb": 8,
	"c-2": 2,
	"c-4": 4,
}


def overprovision_factor() -> float:
	"""Fleet-wide vCPU oversubscription multiplier from Atlas Settings.

	Default 1 (no oversubscription) when unset. A host's effective vCPU budget
	is its physical total times this factor. Applies to the CPU axis only — RAM
	and disk are hard fits."""
	value = frappe.db.get_single_value("Atlas Settings", "overprovision_factor")
	return float(value) if value else 1.0


def host_memory_reserve_megabytes() -> int:
	"""MB carved off every host's memory budget before placement/resize accounting.

	`Atlas Settings.host_memory_reserve_megabytes`, default 1024 when unset. Guest
	RAM must never pack to 100% of MemTotal: the host OS, per-VM Firecracker/jailer
	overhead, and thin-pool metadata live in the same RAM, so packing to MemTotal
	OOMs the host. Subtracted from the memory axis's effective budget only (the raw
	`total` stays physical); CPU and disk are unaffected. An explicit 0 disables it."""
	value = frappe.db.get_single_value("Atlas Settings", "host_memory_reserve_megabytes")
	return int(value) if value is not None else 1024


# The per-VM columns every capacity sum charges against a host, on all three axes.
_VM_COST_FIELDS = (
	"vcpus",
	"cpu_max_cores",
	"memory_megabytes",
	"disk_gigabytes",
	"data_disk_gigabytes",
)


def _incoming_migration_vms(server: str) -> list:
	"""Cost rows for the VMs migrating INTO `server` but not yet cut over.

	A non-terminal `Virtual Machine Migration` with `target_server == server` means
	the target is already hydrating that VM's disk and will boot it; its `vm.server`
	still names the source until Repointing, so `capacity_for_server`'s resident query
	misses it. Return the same `_VM_COST_FIELDS` shape so it sums alongside the
	resident VMs. Skips any whose `server` is already this host (defensive against a
	just-repointed row) so a VM is never counted twice on the same host."""
	from atlas.atlas.doctype.virtual_machine_migration.virtual_machine_migration import (
		TERMINAL_STATUSES,
	)

	names = frappe.get_all(
		"Virtual Machine Migration",
		filters={"target_server": server, "status": ["not in", TERMINAL_STATUSES]},
		pluck="virtual_machine",
	)
	if not names:
		return []
	return frappe.get_all(
		"Virtual Machine",
		filters={"name": ["in", names], "server": ["!=", server], "status": ["!=", "Terminated"]},
		fields=list(_VM_COST_FIELDS),
	)


def _axis(total: float | None, effective: float | None, used: float) -> dict:
	"""A per-resource capacity block.

	`total` is the host's physical amount on this axis (None → uncatalogued);
	`effective` is the budget placement checks against (total x factor for CPU,
	total for RAM/disk; None → unlimited on this axis); `used` is the sum of the
	non-Terminated VMs' cost on this axis."""
	return {"total": total, "effective": effective, "used": used}


def _vcpus_total(server: dict, slug: str | None) -> int | None:
	"""Physical vCPU total for a server: agent-reported `vcpus_total` first, else
	the legacy slug dict. None when neither knows it → uncatalogued CPU axis."""
	if server.get("vcpus_total"):
		return int(server["vcpus_total"])
	return DIGITALOCEAN_VCPUS_BY_SIZE.get(slug) if slug else None


@frappe.whitelist()
def capacity_for_server(server: str) -> dict:
	"""Return per-axis {total, effective, used} for CPU, RAM, and pool disk.

	`used` sums the non-Terminated VMs on this server: CPU by bandwidth cap
	(`cpu_max_cores`, falling back to `vcpus` for older rows — the true cost, so
	fractional-vCPU VMs don't each spend a whole vCPU), RAM by `memory_megabytes`,
	disk by reserved `disk_gigabytes + data_disk_gigabytes` (reserved sum, not
	live pool %, since thin provisioning is the point of the pool). An axis whose
	`total` is unset reports `effective is None` — uncatalogued → unlimited — the
	same per-axis vouch-by-Active rule. `pool_data_percent` is carried through as
	an advisory alert signal, never a placement predicate.
	"""
	s = (
		frappe.db.get_value(
			"Server",
			server,
			[
				"provider_type",
				"size",
				"vcpus_total",
				"memory_megabytes_total",
				"pool_disk_gigabytes_total",
				"pool_data_percent",
			],
			as_dict=True,
		)
		or {}
	)
	size = s.get("size")
	# A Fake host has no agent to report totals, but dev must always see *measured*
	# capacity (never the unreported→sentinel fallback). Synthesize its totals from
	# the Fake size catalog so every axis is catalogued.
	if s.get("provider_type") == "Fake":
		from atlas.atlas.providers.fake import fake_host_totals

		s.update(fake_host_totals(size))
	# Server.size is a Link to Provider Size, stored as "{type}/{slug}". Strip
	# the prefix before the legacy slug-keyed CPU fallback.
	slug = size.split("/", 1)[1] if size and "/" in size else size
	vcpus_total = _vcpus_total(s, slug)
	memory_total = int(s["memory_megabytes_total"]) if s.get("memory_megabytes_total") else None
	disk_total = int(s["pool_disk_gigabytes_total"]) if s.get("pool_disk_gigabytes_total") else None

	resident = frappe.get_all(
		"Virtual Machine",
		filters={"server": server, "status": ["!=", "Terminated"]},
		fields=list(_VM_COST_FIELDS),
	)
	# A VM migrating INTO this host is already spending its budget here: the target
	# hydrates the disk and boots the guest before cutover repoints `vm.server`
	# (spec/24). Until then `vm.server` still names the source, so the resident query
	# misses it — count it against the target too, or placement would double-book a
	# host that is receiving migrations (including the consolidation moves placement
	# itself starts, spec/28). The source keeps counting it (it still runs there until
	# cutover): a migrating VM is deliberately charged to BOTH hosts for its brief
	# life, the safe direction for a capacity gate.
	incoming = _incoming_migration_vms(server)
	vms = resident + incoming
	factor = overprovision_factor()
	# Memory: hard fit, but never packable to the full physical total — the host OS
	# + per-VM VMM overhead live in the same RAM, so effective = total − reserve
	# (clamped ≥ 0). The raw total stays physical for display.
	memory_reserve = host_memory_reserve_megabytes()
	memory_effective = max(0, memory_total - memory_reserve) if memory_total is not None else None
	cpu_axis = _axis(
		total=vcpus_total,
		effective=(vcpus_total * factor) if vcpus_total is not None else None,
		used=sum(float(v.cpu_max_cores or v.vcpus or 0) for v in vms),
	)
	memory_axis = _axis(
		total=memory_total,
		effective=memory_effective,  # total − host_memory_reserve, no oversubscription
		used=sum(int(v.memory_megabytes or 0) for v in vms),
	)
	disk_axis = _axis(
		total=disk_total,
		effective=disk_total,  # no oversubscription
		used=sum(int(v.disk_gigabytes or 0) + int(v.data_disk_gigabytes or 0) for v in vms),
	)
	share = _share_units(cpu_axis, memory_axis, disk_axis)
	return {
		"server": server,
		"size": size,
		"cpu": cpu_axis,
		"memory": memory_axis,
		"disk": disk_axis,
		# Share-unit view of this host (None when no axis is measured) — how many
		# Shared-1x slots fit, how many are used, and what each axis strands.
		"share_units": share["share_units"] if share else None,
		"stranded": share["stranded"] if share else None,
		"pool_data_percent": s.get("pool_data_percent"),  # advisory alert signal
		"virtual_machine_count": len(resident),
		# VMs migrating in but not yet cut over — counted in `used` above, surfaced
		# separately so the operator can see why a host reads fuller than its resident
		# VM list.
		"incoming_migration_count": len(incoming),
	}


def _share_units(cpu_axis: dict, memory_axis: dict, disk_axis: dict) -> dict | None:
	"""The share-unit view of a host: sellable Shared-1x slots and per-axis stranding.

	One share unit is `sizes.SHARE_UNIT` (a Shared 1x). Because every preset is an
	exact whole-number multiple of it (spec/28), a host holds
	`floor(min over measured axes of effective/unit)` units, and any mix of preset
	VMs whose unit-sum ≤ that fits — packing is one-dimensional. `used` is the max
	over measured axes of `ceil(used/unit)` (the binding axis, rounded up so a
	partly-used unit is spent). `stranded` per measured axis is
	`effective − units_total × unit_cost`: the resources the *bottleneck* axis makes
	unsellable at full subscription — the shape-mismatch waste an operator compares
	host shapes by. Reporting only, never a placement input. Returns None when no
	axis is measured (nothing to count against)."""
	priced = {
		"cpu": (cpu_axis, SHARE_UNIT["cpu_max_cores"]),
		"memory": (memory_axis, SHARE_UNIT["memory_megabytes"]),
		"disk": (disk_axis, SHARE_UNIT["disk_gigabytes"]),
	}
	measured = {
		key: (axis, unit) for key, (axis, unit) in priced.items() if axis["effective"] is not None
	}
	if not measured:
		return None
	total = min(int(axis["effective"] // unit) for axis, unit in measured.values())
	used = max((math.ceil(axis["used"] / unit) for axis, unit in measured.values()), default=0)
	stranded = {key: axis["effective"] - total * unit for key, (axis, unit) in measured.items()}
	return {
		"share_units": {"total": total, "used": used, "free": total - used},
		"stranded": stranded,
	}


def _sum_axis(servers: list[dict], key: str) -> dict:
	"""Fleet-wide roll-up of one axis across per-server capacity blocks.

	`total`/`effective` sum only the servers *catalogued* on this axis
	(`effective is not None`); `uncatalogued` counts the rest, whose budget is
	unlimited — so the totals are a floor, not a ceiling. `used` sums across all
	servers regardless."""
	axes = [s[key] for s in servers]
	catalogued = [a for a in axes if a["effective"] is not None]
	return {
		"total": sum(a["total"] for a in catalogued),
		"effective": sum(a["effective"] for a in catalogued),
		"used": sum(a["used"] for a in axes),
		"uncatalogued": len(axes) - len(catalogued),
	}


@frappe.whitelist()
def cluster_capacity() -> dict:
	"""Aggregate `capacity_for_server` across every Active Server, per axis.

	The fleet-wide view behind the per-server one: "how much room does the whole
	cluster have, regardless of which host a VM lands on?" — the same question
	placement asks (atlas/placement.py), summed per axis instead of walked one
	server at a time. Each of `cpu`/`memory`/`disk` carries the summed
	{total, effective, used} plus `uncatalogued` (servers with no known total on
	that axis, treated as unlimited). `servers` carries the per-server breakdown
	for a drill-down.
	"""
	names = frappe.get_all(
		"Server",
		filters={"status": "Active"},
		pluck="name",
		order_by="creation asc",
	)
	servers = [capacity_for_server(name) for name in names]
	fleet_share = _sum_share_units(servers)
	return {
		"server_count": len(servers),
		"cpu": _sum_axis(servers, "cpu"),
		"memory": _sum_axis(servers, "memory"),
		"disk": _sum_axis(servers, "disk"),
		# Fleet share-unit roll-up (None when no server is measured): summed sellable
		# slots and stranded resources across measured hosts — the shape-comparison
		# line the operator reads to decide what host shapes to buy next.
		"share_units": fleet_share["share_units"] if fleet_share else None,
		"stranded": fleet_share["stranded"] if fleet_share else None,
		"virtual_machine_count": sum(s["virtual_machine_count"] for s in servers),
		"servers": servers,
	}


def _sum_share_units(servers: list[dict]) -> dict | None:
	"""Fleet roll-up of per-server share units + stranded resources.

	Sums only servers with a measured `share_units` block; an uncatalogued host is
	unlimited and uncountable, so it is left out (the totals are a floor, like
	`_sum_axis`). Returns None when no server is measured."""
	measured = [s for s in servers if s.get("share_units")]
	if not measured:
		return None
	total = sum(s["share_units"]["total"] for s in measured)
	used = sum(s["share_units"]["used"] for s in measured)
	stranded: dict[str, float] = {}
	for s in measured:
		for axis_key, value in s["stranded"].items():
			stranded[axis_key] = stranded.get(axis_key, 0) + value
	return {
		"share_units": {"total": total, "used": used, "free": total - used},
		"stranded": stranded,
	}
