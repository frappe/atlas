"""The Satellite read API — the surface a Satellite orchestrator polls to register and
sync the VMs an Atlas provisions (spec/28, the provisioner/orchestrator split).

Atlas is a *pure provisioner*: it owns "a VM exists" and nothing about services. A
Satellite is a SEPARATE deployment that manages services itself, over its own SSH to
the hosts and guests Atlas hands over. It never reaches into Atlas's DB — it reads
these methods to mirror the VM/Server it needs and learns the connection details
(the host's public IPv4, the guest's public IPv6) so it can SSH in.

One Satellite federates many Atlasses, so a Satellite holds a per-Atlas credential and
base URL; each Atlas authenticates the caller with THIS Atlas's admin token (System
Manager), exactly like the Central inbound API (`central_link.py`). These are strictly
read-only: the write side of the boundary is Satellite SSHing the box, not calling here.
"""

from __future__ import annotations

import frappe


def _server_ipv4(server: str | None) -> str | None:
	"""The host's public IPv4 — how a Satellite SSHes the HOST (host-plane services:
	the mesh, the gateway). None for a VM not yet placed on a server."""
	if not server:
		return None
	return frappe.db.get_value("Server", server, "ipv4_address")


def _vm_payload(vm) -> dict:
	"""The registration mirror a Satellite keeps for one VM: identity + tenant + the two
	SSH targets (host IPv4, guest IPv6) + base addressing. Deliberately service-free —
	Atlas has no service roles to report.

	`build_mode` (the bake mode of the image, site/admin) and `warm` (was this VM
	warm-restored) are PROVISIONER facts — an image attribute and a boot fact, not
	service state (build_mode stays in Atlas by decision 9). A Satellite that installs a
	site into the guest needs them to pick the deploy mode + gate on the warm identity
	freshen, so they ride the mirror rather than a Central→Satellite call."""
	return {
		"name": vm.name,
		"status": vm.status,
		"server": vm.server,
		"server_ipv4": _server_ipv4(vm.server),
		"tenant": vm.tenant,
		"guest_ipv6": vm.ipv6_address,
		"private_address": vm.private_address,
		"build_mode": vm.build_mode or "",
		"warm": bool(vm.warm_snapshot),
		# The Site/Pilot subdomain label(s) the provisioner recorded as routing intent (the
		# routing dedup): the Satellite creates its own Subdomain rows from these instead of
		# Atlas creating them. A routing fact, but the minimal bridge that lets Atlas stop
		# owning the guest-plane routing while Site/Pilot stay here.
		"routing_subdomains": frappe.parse_json(vm.routing_subdomains or "[]"),
		"modified": str(vm.modified),
	}


@frappe.whitelist()
def get_virtual_machine(name: str) -> dict:
	"""One VM's registration payload (identity, tenant, host IPv4 + guest IPv6). The
	Satellite calls this on a webhook or during a reconcile to (re)populate its mirror."""
	frappe.only_for("System Manager")
	return _vm_payload(frappe.get_doc("Virtual Machine", name))


@frappe.whitelist()
def list_virtual_machines(modified_after: str | None = None) -> list[dict]:
	"""Every VM (optionally only those modified since `modified_after`, an ISO timestamp)
	— the Satellite's reconcile/backfill sweep, so a missed webhook self-heals. Ordered
	oldest-change first so a paging caller can advance its watermark."""
	frappe.only_for("System Manager")
	filters = {"modified": (">", modified_after)} if modified_after else {}
	names = frappe.get_all(
		"Virtual Machine", filters=filters, pluck="name", order_by="modified asc"
	)
	return [_vm_payload(frappe.get_doc("Virtual Machine", n)) for n in names]


@frappe.whitelist()
def get_server(name: str) -> dict:
	"""One host's payload — its public IPv4 (the Satellite's host-plane SSH target) and
	status. Mirrored so a Satellite can address host-plane work without re-reading a VM."""
	frappe.only_for("System Manager")
	server = frappe.get_doc("Server", name)
	return {
		"name": server.name,
		"status": server.status,
		"ipv4": server.ipv4_address,
		"modified": str(server.modified),
	}
