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

import frappe

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

	vms = frappe.get_all(
		"Virtual Machine",
		filters={"server": server, "status": ["!=", "Terminated"]},
		fields=[
			"vcpus",
			"cpu_max_cores",
			"memory_megabytes",
			"disk_gigabytes",
			"data_disk_gigabytes",
		],
	)
	factor = overprovision_factor()
	return {
		"server": server,
		"size": size,
		"cpu": _axis(
			total=vcpus_total,
			effective=(vcpus_total * factor) if vcpus_total is not None else None,
			used=sum(float(v.cpu_max_cores or v.vcpus or 0) for v in vms),
		),
		"memory": _axis(
			total=memory_total,
			effective=memory_total,  # no oversubscription
			used=sum(int(v.memory_megabytes or 0) for v in vms),
		),
		"disk": _axis(
			total=disk_total,
			effective=disk_total,  # no oversubscription
			used=sum(int(v.disk_gigabytes or 0) + int(v.data_disk_gigabytes or 0) for v in vms),
		),
		"pool_data_percent": s.get("pool_data_percent"),  # advisory alert signal
		"virtual_machine_count": len(vms),
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
	return {
		"server_count": len(servers),
		"cpu": _sum_axis(servers, "cpu"),
		"memory": _sum_axis(servers, "memory"),
		"disk": _sum_axis(servers, "disk"),
		"virtual_machine_count": sum(s["virtual_machine_count"] for s in servers),
		"servers": servers,
	}
