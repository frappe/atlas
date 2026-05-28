"""Whitelisted helper used by the Virtual Machine creation form.

Returns "what does this Server have, and how much of it is already spoken for?"
so the operator can see oversubscription before clicking Provision. vCPU totals
come from a small static dict keyed by DigitalOcean size slug — same maintenance
model as `Provider Size.monthly_cost_usd`.
"""

import frappe

# vCPUs per DigitalOcean size slug. Hand-maintained; missing slugs return None
# from `capacity_for_server` and the client falls back to a "—" total.
DIGITALOCEAN_VCPUS_BY_SIZE: dict[str, int] = {
	"s-1vcpu-1gb": 1,
	"s-1vcpu-2gb": 1,
	"s-2vcpu-2gb": 2,
	"s-2vcpu-4gb-intel": 2,
	"s-2vcpu-4gb": 2,
	"s-4vcpu-8gb": 4,
	"c-2": 2,
	"c-4": 4,
}


@frappe.whitelist()
def capacity_for_server(server: str) -> dict:
	"""Return total vs. used vCPUs and VM count for a Server.

	`total` is None when the Server's size slug isn't in the static dict
	(self-managed hosts have no slug); the client renders "—" in that case.
	`used` sums `vcpus` of non-Terminated VMs.
	"""
	size = frappe.db.get_value("Server", server, "size")
	# Server.size is now a Link to Provider Size, stored as "{type}/{slug}".
	# Strip the prefix before looking up vCPUs in the legacy slug-keyed dict.
	slug = size.split("/", 1)[1] if size and "/" in size else size
	total = DIGITALOCEAN_VCPUS_BY_SIZE.get(slug) if slug else None
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
		"used_vcpus": used,
		"virtual_machine_count": len(used_rows),
	}
