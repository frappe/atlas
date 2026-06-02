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
		"Virtual Machine Image", filters={"is_active": 1}, pluck="name", limit=2
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
	(atlas/api/server_capacity.py): a server's vCPU total minus the vCPUs of
	its non-Terminated VMs. Servers whose size has no known vCPU total (e.g.
	self-managed) are treated as having room — the operator vouches for them by
	marking them Active. Raises when nothing fits."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	servers = frappe.get_all(
		"Server", filters={"status": "Active"}, pluck="name", order_by="creation asc"
	)
	if not servers:
		frappe.throw("No capacity available — contact your operator.")
	for server in servers:
		capacity = capacity_for_server(server)
		total = capacity["total_vcpus"]
		if total is None or capacity["used_vcpus"] + required_vcpus <= total:
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
