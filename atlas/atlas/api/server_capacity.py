"""Whitelisted helper used by the Virtual Machine creation form.

Returns "what does this Server have, and how much of it is already spoken for?"
so the operator can see oversubscription before clicking Provision. vCPU totals
come from a small static dict keyed by DigitalOcean size slug — same maintenance
model as `Provider Size.monthly_cost_usd`.

Capacity is deliberately oversubscribable: a VM's `vcpus` is a bandwidth cap
(cpu.max), not a pinned core, so a host can safely back more vCPUs than it
physically has. The fleet-wide multiplier is `Atlas Settings.overprovision_factor`
(default 1 — no oversubscription until the operator raises it). A size we don't
recognize has *no* known total, so we report unlimited capacity and let
placement put a VM there — the operator vouched for the host by marking it
Active (self-managed hosts have no slug at all).
"""

import frappe

# vCPUs per DigitalOcean size slug. Hand-maintained; missing slugs report
# unlimited capacity from `capacity_for_server` and the client falls back to a
# "—" total.
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
	is its physical total times this factor."""
	value = frappe.db.get_single_value("Atlas Settings", "overprovision_factor")
	return float(value) if value else 1.0


@frappe.whitelist()
def capacity_for_server(server: str) -> dict:
	"""Return total vs. used vCPUs and VM count for a Server.

	`total_vcpus` is the host's physical vCPU count; `effective_vcpus` is that
	times `Atlas Settings.overprovision_factor` — the budget placement actually
	checks against. Both are None when the Server's size slug isn't in the
	static dict (self-managed hosts have no slug), which the client renders as
	"—" and placement treats as unlimited. `used` sums `vcpus` of non-Terminated
	VMs.
	"""
	size = frappe.db.get_value("Server", server, "size")
	# Server.size is now a Link to Provider Size, stored as "{type}/{slug}".
	# Strip the prefix before looking up vCPUs in the legacy slug-keyed dict.
	slug = size.split("/", 1)[1] if size and "/" in size else size
	total = DIGITALOCEAN_VCPUS_BY_SIZE.get(slug) if slug else None
	effective = total * overprovision_factor() if total is not None else None
	used_rows = frappe.get_all(
		"Virtual Machine",
		filters={"server": server, "status": ["!=", "Terminated"]},
		fields=["vcpus"],
	)
	used = sum(int(row.vcpus or 0) for row in used_rows)
	return {
		"server": server,
		"size": size,
		"total_vcpus": total,
		"effective_vcpus": effective,
		"used_vcpus": used,
		"virtual_machine_count": len(used_rows),
	}
