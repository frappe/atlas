"""Services handlers for the two operator RPCs that live (as whitelisted methods,
for Desk `frm.call`) on the core VM controller but are pure PaaS actions: stand a
customer gateway's wg0 up, and read a proxy's live maps. Core fires them by name
(`vm.deploy_gateway` / `vm.read_proxy_maps`) and returns the handler's result, so
the VM controller never imports customer_gateway or proxy.
"""

from __future__ import annotations

import frappe


def on_deploy_gateway(vm) -> bool:
	"""Stand up (or re-assert) this gateway VM's wg0 + the static same_48 guard, over
	guest-SSH (spec/26). Gateway-only: a non-gateway VM has no wg0 to bring up."""
	if not vm.is_gateway:
		frappe.throw(f"{vm.name} is not a customer gateway (is_gateway unset)")
	from atlas.atlas import customer_gateway

	return customer_gateway.deploy_gateway(vm.name)


def on_read_proxy_maps(vm) -> dict:
	"""Return this proxy's three live maps (sites / sni / acme) alongside the desired
	maps and a per-map drift flag — read-only. Proxy-only: a non-proxy VM has no
	admin sockets to read."""
	if not vm.is_proxy:
		frappe.throw(f"{vm.name} is not a proxy (is_proxy unset)")
	from atlas.atlas import proxy

	return proxy.read_live_maps(vm.name)
