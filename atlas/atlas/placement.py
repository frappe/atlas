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
from frappe import _


class NoCapacityError(frappe.ValidationError):
	"""No Active server in the region can fit the requested machine.

	Distinct from a generic validation failure so Central — which drives VM
	creates as a service user (spec/16-central.md) after pre-checking
	capability / billing / quota — can tell "region is full, retry / queue /
	alert the operator" apart from "the request itself was bad". Subclasses
	ValidationError, so the user-facing message and HTTP status are unchanged
	for the dashboard path; only the exception type carries the extra signal."""


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
		frappe.throw(_("No image is available — contact your operator."))
	if len(active) > 1:
		frappe.throw(_("Several images are active — ask your operator to set a default image."))
	return active[0]


def default_server(required_vcpus: float) -> str:
	"""The first Active server with room for `required_vcpus`.

	`required_vcpus` is a CPU *bandwidth* cost (cpu_max_cores units), matching
	how `capacity_for_server` sums usage — a 1/16-vCPU machine needs 0.0625, not
	a whole vCPU. Capacity is the same accounting the desk capacity helper uses
	(atlas/api/server_capacity.py): a server's *effective* vCPU budget (physical
	total times `Atlas Settings.overprovision_factor`) minus the bandwidth of its
	non-Terminated VMs. Servers whose size has no known vCPU total — a size we
	haven't catalogued, or a self-managed host with no slug — report
	`effective_vcpus is None` and are treated as having unlimited room: the
	operator vouches for them by marking them Active. Raises when nothing fits.

	Runs with ignore_permissions: this is system placement, not desk RBAC —
	Central (the operator) triggers it without needing Server read access; the
	system still has to choose one."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	servers = frappe.get_all(
		"Server",
		filters={"status": "Active"},
		pluck="name",
		order_by="creation asc",
		ignore_permissions=True,
	)
	if not servers:
		frappe.throw(_("No capacity available — contact your operator."), NoCapacityError)
	for server in servers:
		capacity = capacity_for_server(server)
		budget = capacity["effective_vcpus"]
		if budget is None or capacity["used_vcpus"] + required_vcpus <= budget:
			return server
	frappe.throw(_("No capacity available — contact your operator."), NoCapacityError)


def apply_user_defaults(virtual_machine) -> None:
	"""Fill `server` and `image` on a VM that a user created without them.

	No-op when both are already set (the operator path, or a retry). Called
	from VirtualMachine.before_insert."""
	if virtual_machine.image and virtual_machine.server:
		return
	if not virtual_machine.image:
		virtual_machine.image = default_image()
	if not virtual_machine.server:
		# Bandwidth cost, matching capacity_for_server's used sum. before_validate
		# defaults cpu_max_cores to vcpus, but apply_user_defaults runs in
		# before_insert (before before_validate), so fall back to vcpus here too.
		required = float(virtual_machine.cpu_max_cores or virtual_machine.vcpus or 1)
		virtual_machine.server = default_server(required)


def default_bench_snapshot() -> str:
	"""The golden bench Virtual Machine Snapshot a self-serve Site clones from.

	A `Site`'s backing VM is not laid down from a base image — it is cloned from
	the snapshot baked by the golden image (spec/08-images.md, preinstalled bench + MariaDB + Redis), via
	`Virtual Machine Snapshot.clone_to_new_vm`. The operator names that snapshot
	in `Atlas Settings.default_bench_snapshot`. Fail loud at the boundary when it
	is unset or no longer Available — a Site can't be provisioned without it."""
	configured = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	if not configured:
		frappe.throw(_("No golden bench snapshot is configured — contact your operator."))
	status = frappe.db.get_value("Virtual Machine Snapshot", configured, "status")
	if status is None:
		frappe.throw(f"Configured bench snapshot {configured} does not exist — contact your operator.")
	if status != "Available":
		frappe.throw(f"Bench snapshot {configured} is not Available (status is {status}).")
	return configured


def warm_bench_snapshot_for_server(server: str) -> str | None:
	"""The warm golden this server can fan out from, or None (→ cold clone).

	Warm snapshots are PER-SERVER: a Firecracker memory snapshot only restores on
	the CPU/kernel/Firecracker it was captured on, so the artifact lives (and is
	resolved) by server — unlike `default_bench_snapshot`, the single cold
	fallback pointer. Newest Available wins (the bake supersedes older rows, so
	there is normally exactly one). This is an OPTIMISTIC pick: the authoritative
	compatibility gate is vm-restore.py's host-signature guard on the server
	itself, which cold-boots the clone when the host drifted (e.g. a DigitalOcean
	live migration) — so a stale row costs one cold boot, never a wrong restore."""
	rows = frappe.get_all(
		"Virtual Machine Snapshot",
		filters={"server": server, "kind": "Warm", "status": "Available"},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)
	return rows[0] if rows else None


def atlas_region() -> str:
	"""This Atlas instance's single region — the one source of truth.

	Read off `Atlas Settings.region`. The same string is the cert-dir scope on every
	proxy guest, the separator that names this bench's servers in a shared cloud
	account, the region `Root Domain` denormalizes at insert, and the region
	announced to Central at Register. Atlas is single-region, so there is exactly one
	value — Subdomain/Site/Port Mapping/proxy VMs no longer carry a denormalized copy;
	they belong to the one region by definition. Fail loud at the boundary (Taste 17)
	when it is unset — every region-dependent path needs it, and a blank would surface
	far later as a cryptic mismatch."""
	region = frappe.db.get_single_value("Atlas Settings", "region")
	if not region:
		frappe.throw(_("Set Atlas Settings.region (this Atlas's region) — contact your operator."))
	return region


def active_root_domain() -> "frappe.model.document.Document":
	"""The single active Root Domain a self-serve Site is fronted by.

	A `Root Domain` row (e.g. `blr1.frappe.dev`) ties a region to its regional
	wildcard zone — the exact thing the proxy fleet terminates. A Site resolves
	this once at insert to derive both its `region` and its FQDN suffix; the user
	never picks either. Atlas is single-region today, so this is the one active
	row. Raises (fail loud) when none or several are active — placement, like the
	image/server choice, must be unambiguous."""
	active = frappe.get_all(
		"Root Domain",
		filters={"is_active": 1},
		fields=["name", "domain", "region"],
		limit=2,
		ignore_permissions=True,
	)
	if not active:
		frappe.throw(_("No domain is configured — contact your operator."))
	if len(active) > 1:
		frappe.throw(_("Several domains are active — ask your operator to set a single active domain."))
	return frappe.get_doc("Root Domain", active[0]["name"])
