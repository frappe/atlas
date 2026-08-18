"""The PaaS side of a VM terminate — releasing the routing/proxy/gateway
artifacts a VM held so terminating it leaves nothing stranded (spec/18 Component
F, spec/24, spec/26). Registered on core's `vm.terminated`; core fires it and
does not know these concerns exist.

Moved off `vm_teardown.py` (which now keeps only the core teardown — reserved
IP + snapshots): a Subdomain/Custom Domain/VPN/proxy/front-door is a services
concept. `on_vm_terminated` runs them in the same order the controller's
`terminate()` did.
"""

from __future__ import annotations

import frappe


def on_vm_terminated(vm) -> None:
	"""The services teardown a terminate fires, in the order `terminate()` ran
	them: revoke tunnels + VPC peers, drop the Subdomains + Custom Domains, drop
	the proxy role, then mark the front doors Terminated. Each step is idempotent
	and best-effort."""
	revoke_tunnels(vm)
	revoke_vpc_peers(vm)
	delete_subdomains(vm)
	delete_custom_domains(vm)
	deprovision_proxy(vm)
	terminate_front_doors(vm)


def delete_subdomains(vm) -> None:
	"""Drop every Subdomain that routes to this VM, so terminating it stops routing
	(each row's on_trash deconverges the regional proxy fleet). The leak fix
	(spec/18 Component F): today ONLY `Site.terminate` cleans up Subdomains, so a VM
	terminated directly — by the operator, or any non-`Site` path (a bench VM,
	`Site.terminate`'s own backing-VM teardown after it already cleared its one
	Subdomain) — would otherwise strand its routes on a /128 that `allocate_ipv6`
	re-hands to the next tenant, a cross-tenant traffic leak.

	A `Subdomain` is the LINKER of the VM (its `virtual_machine` field points AT this
	VM), so nothing on the VM side obstructs the delete. But a bench VM's Subdomain is
	itself linked-TO by the `Pilot` that fronts it (`subdomain_doc`), and a self-serve
	site's by its `Site` (`subdomain_doc`) — and Frappe's link-integrity guard protects
	that linked-TO doc, so deleting the Subdomain out from under a live Pilot/Site raises
	`LinkExistsError`. The aggregate's own teardown (`site_common.delete_subdomain`) clears
	its `subdomain_doc` before deleting, but a VM terminated directly (the operator,
	or Central's `terminate_server` driving the VM's own `terminate`) bypasses those
	paths, so we clear the referencing link here first — the same clear-then-delete order,
	from the side that owns the Subdomain rather than the side that references it.
	Idempotent: a VM with no Subdomains is a no-op.
	`terminate()` is the ONLY controller-side teardown — there is deliberately NO
	scheduled sweeper backstop (spec/18 Component F, "Why no sweeper"): because this
	deletes a VM's rows in the same teardown that releases its /128, a row never
	outlives its VM's address, so the case a sweeper would catch is closed here."""
	for name in frappe.get_all("Subdomain", filters={"virtual_machine": vm.name}, pluck="name"):
		clear_subdomain_references(name)
		frappe.delete_doc("Subdomain", name, ignore_permissions=True)


def clear_subdomain_references(subdomain: str) -> None:
	"""Null out any `Pilot`/`Site` `subdomain_doc` Link pointing at `subdomain`, so the
	link-integrity guard lets the Subdomain be deleted. The null must be persisted
	(db_set) before the delete, since the guard queries the DB — mirrors the db_set order
	in `site_common.delete_subdomain`."""
	for doctype in ("Pilot", "Site"):
		for name in frappe.get_all(doctype, filters={"subdomain_doc": subdomain}, pluck="name"):
			frappe.db.set_value(doctype, name, "subdomain_doc", None)


def delete_custom_domains(vm) -> None:
	"""Drop every Custom Domain that routes to this VM, so terminating it stops routing
	(each row's on_trash deconverges the regional proxy fleet's custom-domain map). The
	full-FQDN sibling of `delete_subdomains` (spec/18 Phase 2): a custom domain is the
	LINKER (its `virtual_machine` points AT this VM), so deletion is unobstructed by the
	link-integrity guard. Idempotent: a VM with no Custom Domains is a no-op. Like the
	Subdomain teardown, this is part of the SAME teardown that releases the VM's /128, so
	a custom-domain route never outlives its VM's address (Component F)."""
	for name in frappe.get_all("Custom Domain", filters={"virtual_machine": vm.name}, pluck="name"):
		frappe.delete_doc("Custom Domain", name, ignore_permissions=True)


def revoke_tunnels(vm) -> None:
	"""Revoke every VPN Tunnel to this VM on terminate (spec/19-vpn-broker.md).
	terminate-vm.py tears down the VM's netns/veth but the tunnel's wg interface
	lives in the host ROOT netns and survives that, so each tunnel's revoke()
	runs the host down Task to remove it. Idempotent: a VM with no tunnels is a
	no-op; already-Revoked tunnels are skipped."""
	for name in frappe.get_all(
		"VPN Tunnel",
		filters={"virtual_machine": vm.name, "status": ["!=", "Revoked"]},
		pluck="name",
	):
		frappe.get_doc("VPN Tunnel", name).revoke()


def revoke_vpc_peers(vm) -> None:
	"""Revoke every VPN Peer this VM terminates as a gateway (spec/26). A
	terminated gateway's peers are dead — drop each from the (gone) wg0 and withdraw
	its /128 from the mesh. revoke_peer skips the wg0 push for a Terminated gateway (the
	peers are already gone with the VM) and only withdraws the mesh /128. Idempotent:
	a non-gateway VM has no peers; already-Revoked peers are skipped."""
	# The customer gateway (spec/26) is a later feature than the VM lifecycle: a site
	# may not have migrated the `VPN Peer` DocType. Its absence means "no peers"
	# — never block a terminate on it.
	if not frappe.db.exists("DocType", "VPN Peer"):
		return
	for name in frappe.get_all(
		"VPN Peer",
		filters={"gateway": vm.name, "status": ["!=", "Revoked"]},
		pluck="name",
	):
		frappe.get_doc("VPN Peer", name).revoke()


def deprovision_proxy(vm) -> None:
	"""If this VM fronted traffic as a proxy, drop it out of the fleet on terminate
	so its dead `/128` stops being published in the regional wildcard AAAA set (else
	half the round-robin blackholes into a VM whose guest is gone). Clear `is_proxy`
	and re-publish the wildcard: `status` is already "Terminated" above, so
	`wildcard_targets()` now excludes this VM and the upsert drops its address. No-op
	for a non-proxy VM. A DNS failure is logged inside `_publish_wildcard`, not raised
	— it must not wedge the rest of teardown."""
	if not vm.is_proxy:
		return
	vm.db_set("is_proxy", 0)
	from atlas.atlas.core.placement import active_root_domain

	domain = active_root_domain().domain
	cert_name = frappe.db.get_value("TLS Certificate", {"root_domain": domain, "status": "Active"}, "name")
	if cert_name:
		frappe.get_doc("TLS Certificate", cert_name)._publish_wildcard()


def terminate_front_doors(vm) -> None:
	"""Mark every aggregate backed by this VM Terminated, and push that status.

	A `Pilot`/`Site` is the AUTHORITATIVE status Central mirrors for a bench VM
	(`front_door.FrontDoor.status`, `central_report.on_vm_update`, which suppresses the
	raw VM status for exactly these VMs). The aggregate→VM direction was wired —
	`Pilot.terminate()` tears down its VM — but the VM→aggregate direction was not, so
	terminating the VM DIRECTLY left the aggregate claiming Running forever. That is
	not a corner: it is what Central's own `terminate_server` does (it invokes the
	action on the Virtual Machine), and what the desk's Terminate button on a VM does.

	Nothing corrected it afterwards, either: `vm.deleted` fires from `on_trash` and
	Atlas never deletes the row (terminate is a status flip), and the reconcile pull
	(`api.inventory.tenant_vms`) reports the FRONT DOOR's status — so it re-asserted
	Running rather than repairing it. The result was a tenant seeing a dead server
	listed as Running, with Open minting a session for a gateway that answers nothing.

	Both aggregates, not just the handoff owner: a self-serve VM carries a Site AND
	its attached Pilot (`front_doors_for_vm`), and leaving either behind reproduces the
	bug on that half. `db_set` skips `on_update`, so the status event is emitted
	explicitly — the same gap `report_pilot_status` / `report_site_status` exist to
	close. Delivery is best-effort by design (a queued POST); a Central that is down
	must not fail a terminate, so emission is guarded but the status write is not.

	Skipped when the aggregate is the one doing the terminating (`Pilot.terminate()` /
	`Site.terminate()` set `flags.front_door_terminating` before calling us): it marks
	and saves itself, and re-marking here would fire the same event twice."""
	if vm.flags.get("front_door_terminating"):
		return
	from atlas.atlas.services.front_door import front_doors_for_vm

	for door in front_doors_for_vm(vm.name):
		doc = door.doc
		if doc.status == "Terminated":
			continue
		doc.db_set("status", "Terminated")
		try:
			from atlas.atlas.services import reporting

			# A console (a Pilot, or a Site whose kind is pilot-console) reports AS its
			# VM (vm.status_changed); only a bench-site reports site.status_changed.
			if doc.doctype == "Pilot" or doc.get("kind") == "pilot-console":
				reporting.report_pilot_status(doc)
			else:
				reporting.report_site_status(doc)
		except Exception:
			# The status is already persisted; a reporting failure must not undo the
			# terminate. Central's own reconcile now reads the corrected status.
			frappe.log_error(title=f"front-door terminate report failed: {doc.doctype} {doc.name}")
