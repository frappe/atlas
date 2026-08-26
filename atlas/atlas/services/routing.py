"""Routing side of a VM address change — the services handler core fires when a
migration cutover (change-address) or a forward-collapse moves a VM onto a new
/128. Registered on core's `vm.address_changed`; core never imports this.

This is the body that used to live in `migration._repoint_routes`, moved to the
services side of the line: it rewrites the Subdomain denorm and reconciles the
proxy fleet — both pure PaaS concerns a PaaS-blind core knows nothing about.
"""

from __future__ import annotations

import frappe


def on_vm_address_changed(virtual_machine: str, new_ipv6: str) -> None:
	"""Rewrite every Subdomain's denormalized address to the VM's new /128 via
	db_set (the field is read_only + only refreshed inside validate's
	_denormalize_address, so a plain save wouldn't change it predictably), then
	reconcile the whole proxy fleet (each proxy holds the whole map; there is no
	per-region push). Idempotent."""
	from atlas.atlas.services.proxy import reconcile_proxies

	changed = False
	for row in frappe.get_all(
		"Subdomain",
		filters={"virtual_machine": virtual_machine},
		fields=["name", "address"],
	):
		if row.address != new_ipv6:
			frappe.db.set_value("Subdomain", row.name, "address", new_ipv6)
			changed = True
	if changed:
		# reconcile_proxies tolerates a wedged/empty fleet (per-proxy failure
		# isolation), so this never strands the migration.
		reconcile_proxies()
