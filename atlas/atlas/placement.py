"""Default server + image for a Virtual Machine created without them.

A dashboard user (see spec/11-user-ui.md) never picks where their machine
runs — they state name, size, and SSH key, and the controller fills `server`
and `image` here. The operator still owns the fleet: which Servers are Active
and which Image is the default are operator decisions. This is placement, not
scheduling — first Active server with room, no balancing.

Operators creating a VM in Desk supply `server`/`image` explicitly, so this
never runs for them.
"""

import frappe


def default_image() -> str:
	"""The base image a user's machine provisions from.

	Prefers `Atlas Settings.default_user_image`; otherwise the single active
	image. Raises a user-facing message when the choice is ambiguous or there
	is none — fail loud at the boundary (Taste 17)."""
	configured = frappe.db.get_single_value("Atlas Settings", "default_user_image")
	if configured:
		return configured
	active = frappe.get_all(
		"Virtual Machine Image",
		filters={"is_active": 1},
		pluck="name",
		limit=2,
		ignore_permissions=True,
	)
	if not active:
		frappe.throw("No image is available — contact your operator.")
	if len(active) > 1:
		frappe.throw(
			"Several images are active — ask your operator to set a default image."
		)
	return active[0]


def default_server(required_vcpus: int) -> str:
	"""The first Active server with room for `required_vcpus`.

	Capacity is the same vCPU accounting the desk capacity helper uses
	(atlas/api/server_capacity.py): a server's *effective* vCPU budget (physical
	total times `Atlas Settings.overprovision_factor`) minus the vCPUs of its
	non-Terminated VMs. Servers whose size has no known vCPU total — a size we
	haven't catalogued, or a self-managed host with no slug — report
	`effective_vcpus is None` and are treated as having unlimited room: the
	operator vouches for them by marking them Active. Raises when nothing fits.

	Runs with ignore_permissions: this is system placement, not user-facing
	data access. The Atlas User who triggers it cannot read Server at all (by
	design) — but the system still has to choose one for them."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	servers = frappe.get_all(
		"Server",
		filters={"status": "Active"},
		pluck="name",
		order_by="creation asc",
		ignore_permissions=True,
	)
	if not servers:
		frappe.throw("No capacity available — contact your operator.")
	for server in servers:
		capacity = capacity_for_server(server)
		budget = capacity["effective_vcpus"]
		if budget is None or capacity["used_vcpus"] + required_vcpus <= budget:
			return server
	frappe.throw("No capacity available — contact your operator.")


def apply_user_defaults(virtual_machine) -> None:
	"""Fill `server` and `image` on a VM that a user created without them.

	No-op when both are already set (the operator path, or a retry). Called
	from VirtualMachine.before_insert."""
	if virtual_machine.image and virtual_machine.server:
		return
	if not virtual_machine.image:
		virtual_machine.image = default_image()
	if not virtual_machine.server:
		virtual_machine.server = default_server(int(virtual_machine.vcpus or 1))
