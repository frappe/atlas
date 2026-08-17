"""VM terminate teardown — releasing the routes, addresses, and artifacts a VM
holds so terminating it leaves nothing stranded (spec/18 Component F, spec/24).

Extracted from the `Virtual Machine` controller: the teardown fan-out is one
reason-to-change (what a terminate must clean up), separate from the lifecycle
state machine. `terminate()` calls these in order; each is idempotent and
best-effort. Free functions taking the VM, following the `migration.py` pattern.
"""

from __future__ import annotations

import frappe


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
	`LinkExistsError`. Both `Pilot._delete_subdomain` and `Site._delete_subdomain` clear
	their own `subdomain_doc` before deleting, but a VM terminated directly (the operator,
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
	in `Pilot._delete_subdomain` / `Site._delete_subdomain`."""
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
	from atlas.atlas.placement import active_root_domain

	domain = active_root_domain().domain
	cert_name = frappe.db.get_value("TLS Certificate", {"root_domain": domain, "status": "Active"}, "name")
	if cert_name:
		frappe.get_doc("TLS Certificate", cert_name)._publish_wildcard()


def detach_reserved_ip(vm) -> None:
	"""Release the VM's attached public IPv4 (if any) back to its Server's
	pool on terminate, so the address can be re-attached to another VM. The
	Reserved IP row survives — only the attachment is cleared."""
	for name in frappe.get_all("Reserved IP", filters={"virtual_machine": vm.name}, pluck="name"):
		frappe.get_doc("Reserved IP", name).detach()


def delete_snapshots(vm) -> None:
	"""Drop this VM's snapshot rows after terminate. Each row's on_trash
	lvremoves its snapshot LV — snapshot LVs live in the thin pool, OUTSIDE
	the VM directory terminate-vm.py rm -rf'd, so they survive that and must
	be removed via the per-snapshot delete path (one SSH round trip each;
	the script is idempotent).

	The golden bench snapshot is the exception: it is a DURABLE artifact that
	outlives its build VM — every self-serve site clones from it. Terminating the
	build VM (the bake leaves it as scratch) must NOT take the golden with it, or
	the snapshot row stays "Available" while its LV is gone and the next clone
	fails late in provision-vm.py ("snapshot LV not found"). So skip the snapshot
	currently referenced by Atlas Settings.default_bench_snapshot — and every
	Available WARM snapshot, the same durable-artifact contract: a warm golden is
	the per-server fan-out source and outlives its build VM by design (its own
	on_trash removes the LV + memory pair when the operator retires it).

	A snapshot RECORDED AS AN IMAGE BUILD'S OUTPUT is durable for the same reason,
	and this is the case the two exceptions above missed. A COLD bake's snapshot is
	neither the registered golden nor warm, so `terminate_build_vm` — an ordinary
	checkbox on the very form that offers **Promote** — used to delete the artifact
	the promote needs, seconds after the build reported `Available`. What was left
	was an Image Build pointing at a row that no longer exists, so Promote failed
	with a bare "Virtual Machine Snapshot <name> not found" that reads as data
	corruption rather than "you asked for this". Two staging bakes died that way
	before the cause was found. Retiring a promoted image is the operator's
	explicit call (delete the snapshot, or the Image Build first), never a side
	effect of reaping the scratch VM."""
	golden = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	for row in frappe.get_all(
		"Virtual Machine Snapshot",
		filters={"virtual_machine": vm.name},
		fields=["name", "kind", "status"],
	):
		if row.name == golden:
			continue
		if row.kind == "Warm" and row.status == "Available":
			continue
		if frappe.db.exists("Image Build", {"snapshot": row.name}):
			continue
		# force=1: delete_doc runs on_trash (host artifact removal,
		# non-transactional) BEFORE the link check, so a plain delete on a linked
		# row would destroy the artifacts and then abort on the link, stranding
		# the row. Nothing that survives to here is an Image Build's output (the
		# guard above), so force only covers incidental links.
		frappe.delete_doc("Virtual Machine Snapshot", row.name, ignore_permissions=True, force=1)


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
	from atlas.atlas.front_door import front_doors_for_vm

	for door in front_doors_for_vm(vm.name):
		doc = door.doc
		if doc.status == "Terminated":
			continue
		doc.db_set("status", "Terminated")
		try:
			from atlas.atlas import central_report

			if doc.doctype == "Pilot":
				central_report.report_pilot_status(doc)
			else:
				central_report.report_site_status(doc)
		except Exception:
			# The status is already persisted; a reporting failure must not undo the
			# terminate. Central's own reconcile now reads the corrected status.
			frappe.log_error(title=f"front-door terminate report failed: {doc.doctype} {doc.name}")
