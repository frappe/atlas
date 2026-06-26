"""Central-facing site provisioning — the entry point Central calls to create a
self-serve site for a tenant (spec/14-self-serve.md, spec/16-central.md).

Central owns end-users; it talks to Atlas as the operator (token auth as the
Central service user). It supplies *what* (the tenant it belongs to + the
subdomain label), never *where* — the region resolves from the active Root
Domain (Atlas Settings.region), and placement/clone are the Site controller's
concern. The insert's `after_insert` enqueues `auto_provision`, so the site
clones the golden bench, deploys, and routes itself.

This is the write half of the Central↔Atlas site contract; the read half is
`get_site` (poll) plus the `site.*` events Atlas pushes to Central
(atlas/atlas/central_report.py). There is no email, no User, no verification —
Central already authenticated the tenant.
"""

from __future__ import annotations

import frappe

from atlas.atlas.doctype.tenant.tenant import ensure_tenant


@frappe.whitelist()
def create_site(
	team: str,
	subdomain: str,
	email: str | None = None,
	region: str | None = None,
) -> dict:
	"""Provision a self-serve site for a Central team and return its mirror row.

	`team` is the Central `Team.name`; `email` seeds the Tenant on first use (the
	team owner). The `subdomain` is the single DNS label the site is
	fronted at (`<subdomain>.<region domain>`); the Site controller enforces the
	Contract-A label rules and the authoritative FQDN uniqueness, throwing a clean
	"already taken" the caller can surface. `region` is optional — it defaults to
	the active region (Atlas Settings.region); Central never has to pick it.

	Runs with `ignore_permissions`: operator orchestration authorized by the
	Central token, not desk RBAC. Returns immediately with status `Pending`; the
	clone→deploy→route work runs in the background (`Site.auto_provision`) and is
	reported to Central via `site.*` events / `get_site` polling.
	"""
	tenant = ensure_tenant(team, email)

	doc = {"doctype": "Site", "subdomain": subdomain, "tenant": tenant}
	if region:
		doc["region"] = region
	site = frappe.get_doc(doc).insert(ignore_permissions=True)

	return _mirror(site)


@frappe.whitelist()
def get_site(name: str) -> dict:
	"""Return the current state of a site so Central can poll for progress.

	The poll fallback to the pushed `site.*` events: Central can call this to
	learn a site reached `Running` (and read the admin password + live URL) even
	if an event delivery was missed. Operator-authorized (Central token); no
	owner gating (Atlas no longer owns end-users)."""
	return _mirror(frappe.get_doc("Site", name))


def _mirror(site) -> dict:
	"""The shape Central reflects: identity + lifecycle + (once Running) the
	tenant handoff (admin password + live URL). The admin password is only
	surfaced once the site is serving — before that there is nothing to hand
	off, and the field may not yet be stamped."""
	running = site.status == "Running"
	# The Tenant `name` *is* the Central `Team.name`, so the Site's `tenant` link is
	# the owning team directly; None for operator/e2e sites.
	return {
		"name": site.name,
		"team": site.tenant or None,
		"subdomain": site.subdomain,
		"region": site.region,
		"status": site.status,
		"fqdn": site.name,
		"url": f"https://{site.name}" if running else None,
		"admin_password": site.get_password("admin_password") if running else None,
	}
