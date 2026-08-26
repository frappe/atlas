"""VM terminate teardown, core side — releasing the addresses and disk artifacts
a VM holds so terminating it leaves nothing stranded (spec/24).

The PaaS teardown (Subdomains, Custom Domains, VPN tunnels/peers, proxy role,
front doors) moved to `services/teardown.py`, fired via core's `vm.terminated`
callback so a PaaS-blind `terminate()` never names them. What stays here is the
core cleanup: the attached Reserved IP and the VM's snapshots. Free functions
taking the VM; `terminate()` calls them, each idempotent and best-effort.
"""

from __future__ import annotations

import frappe


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
