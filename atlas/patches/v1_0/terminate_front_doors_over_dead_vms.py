"""Mark every Pilot/Site that outlived its backing VM Terminated.

Until `VirtualMachine._terminate_front_doors` existed, terminating a VM directly — which
is what Central's `terminate_server` does, and the desk's Terminate button on a VM — left
the owning aggregate claiming `Running` forever. Nothing corrected it afterwards: Atlas
never deletes the VM row (terminate is a status flip, so `vm.deleted` never fired), and
the reconcile pull reports the FRONT DOOR's status, so it re-asserted Running instead of
repairing it. Tenants saw dead servers listed as Running.

The code fix stops new ones accruing; this repairs the rows already stranded. No events
are emitted here — the periodic reconcile reads `front_door.status` (`api.inventory
.tenant_vms`), so Central's mirror converges on its own once Atlas's own truth is right,
without a migrate-time event storm.

A missing VM row counts as dead too: the aggregate can never serve again either way.
"""

import frappe


def execute():
	for doctype in ("Pilot", "Site"):
		for row in frappe.get_all(
			doctype,
			filters={"status": ("!=", "Terminated")},
			fields=["name", "virtual_machine"],
		):
			if not row.virtual_machine:
				# Never got a VM (failed before provisioning). Its own status already
				# tells that story; nothing to reconcile against.
				continue
			vm_status = frappe.db.get_value("Virtual Machine", row.virtual_machine, "status")
			if vm_status is not None and vm_status != "Terminated":
				continue
			frappe.db.set_value(doctype, row.name, "status", "Terminated", update_modified=False)
