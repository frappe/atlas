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


def _fits(axis: dict, need: float) -> bool:
	"""Does `need` more of this resource fit on this axis?

	`effective is None` means the host is uncatalogued on this axis → unlimited
	room (the operator vouched for it by marking it Active), so anything fits.
	Otherwise the axis fits when used + need stays within the effective budget."""
	return axis["effective"] is None or axis["used"] + need <= axis["effective"]


def _lock_host(server: str) -> None:
	"""Row-lock the host until the enclosing request commits, serialising placement onto it.

	Capacity is *checked* (read the host's usage) and *consumed* (insert/resize the VM) as
	two steps; without a lock between them, two concurrent callers both read the same free
	room and both place on it (a check-then-act / TOCTOU race → an over-committed host that
	fails at boot instead of a clean rejection). Every placement path — create
	(`default_server`) and resize (`ensure_resize_capacity`) — takes this same `Server`-row
	lock first, so the second caller blocks here, then re-reads usage and sees the first's
	machine. Keyed by host, so placements onto *different* hosts still run in parallel."""
	frappe.db.get_value("Server", server, "name", for_update=True)


def default_server(
	required_vcpus: float,
	required_memory_mb: float,
	required_disk_gb: float,
) -> str:
	"""The first Active server with room on all three axes: CPU, RAM, pool disk.

	`required_vcpus` is a CPU *bandwidth* cost (cpu_max_cores units), matching how
	`capacity_for_server` sums usage — a 1/16-vCPU machine needs 0.0625, not a
	whole vCPU. `required_memory_mb` and `required_disk_gb` are the VM's memory
	and reserved disk (root + data). Capacity is the same accounting the desk
	capacity helper uses (atlas/api/server_capacity.py): each axis's *effective*
	budget minus what its non-Terminated VMs already spend, and a VM is placed
	only where it fits on *every* axis. An axis with no known total — the agent
	hasn't reported it, or (for CPU) the size isn't catalogued — reports
	`effective is None` and is unlimited on that axis: the operator vouches for
	the host by marking it Active. Raises when nothing fits on all three.

	Runs with ignore_permissions: this is system placement, not desk RBAC —
	Central (the operator) triggers it without needing Server read access; the
	system still has to choose one.

	Concurrency-safe: candidates are scanned cheaply first, then the chosen host is
	row-locked (`_lock_host`) and its usage re-read *under the lock* before we commit to
	it — so two simultaneous creates can't both read the same free room and double-book a
	host. The loser blocks on the lock, re-reads, sees the winner's VM, and moves on (or
	raises). The lock is held to the request commit, which for a create is fast (the VM
	insert enqueues provisioning; no slow host work runs inline)."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	servers = frappe.get_all(
		"Server",
		filters={"status": "Active"},
		pluck="name",
		order_by="creation asc",  # consistent lock order → no deadlock between placers
		ignore_permissions=True,
	)
	if not servers:
		frappe.throw(_("No capacity available — contact your operator."), NoCapacityError)

	def fits(capacity: dict) -> bool:
		return (
			_fits(capacity["cpu"], required_vcpus)
			and _fits(capacity["memory"], required_memory_mb)
			and _fits(capacity["disk"], required_disk_gb)
		)

	for server in servers:
		if not fits(capacity_for_server(server)):
			continue  # cheap unlocked skip; the winner is re-checked under the lock below
		_lock_host(server)
		if fits(capacity_for_server(server)):  # authoritative re-read while holding the lock
			return server
		# A concurrent create won this host between the skip-check and the lock — try the next.
	frappe.throw(_("No capacity available — contact your operator."), NoCapacityError)


# Sentinel free-headroom for an axis whose host total is unmeasured (agent hasn't
# reported it). "Unlimited" is real to placement but useless as a number to
# Central, so we hand back an obviously-fake large value and flag the whole shape
# `unmeasured` — Central treats it as "effectively unlimited", never as a fact.
_UNMEASURED_VCPUS = 1024
_UNMEASURED_MEMORY_MB = 1024 * 1024  # 1 TiB
_UNMEASURED_DISK_GB = 1024 * 1024  # 1 PiB


def _axis_free(axis: dict, sentinel: float) -> tuple[float, bool]:
	"""Free headroom on one axis, and whether it's measured.

	Measured axis → `effective - used` (clamped at 0). Uncatalogued axis
	(`effective is None`) → the sentinel, flagged unmeasured."""
	if axis["effective"] is None:
		return sentinel, False
	return max(0.0, axis["effective"] - axis["used"]), True


def largest_vm() -> dict | None:
	"""The largest single VM shape provisionable right now, or None if nothing fits.

	"Largest" is the free headroom (`effective - used` per axis) on the single
	*best* Active host — best = the most total free resources. That triple is a
	genuinely co-schedulable shape: all three axes are simultaneously free on that
	one host, so any VM whose cpu/memory/disk are each within it fits there (a VM
	can't span hosts, so a fleet sum would be a lie). An axis the agent hasn't
	measured contributes a large sentinel and marks the shape `unmeasured`.

	Returns `{vcpus, memory_megabytes, disk_gigabytes, unmeasured}` for the winner,
	or None when there is no Active host at all. Central asks this in resources; it
	never sees hosts."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	servers = frappe.get_all(
		"Server",
		filters={"status": "Active"},
		pluck="name",
		order_by="creation asc",
		ignore_permissions=True,
	)
	if not servers:
		return None

	best = None
	for server in servers:
		c = capacity_for_server(server)
		free_cpu, m_cpu = _axis_free(c["cpu"], _UNMEASURED_VCPUS)
		free_mem, m_mem = _axis_free(c["memory"], _UNMEASURED_MEMORY_MB)
		free_disk, m_disk = _axis_free(c["disk"], _UNMEASURED_DISK_GB)
		measured = m_cpu and m_mem and m_disk
		# Rank measured hosts ahead of unmeasured ones: a real free-headroom shape
		# beats a sentinel one, so a fully-reported host always defines largest_vm
		# when one exists — an unmeasured host only wins when NO measured host can.
		# (Without this, the astronomical sentinels would dwarf any real host's
		# score and hide it behind a fake shape.) Within a class, most total free
		# resources wins; memory dominates the raw MB sum, fine as a tiebreak.
		score = (1 if measured else 0, free_cpu + free_mem + free_disk)
		shape = {
			"vcpus": int(free_cpu),
			"memory_megabytes": int(free_mem),
			"disk_gigabytes": int(free_disk),
			"unmeasured": not measured,
		}
		if best is None or score > best[0]:
			best = (score, shape)
	return best[1]


def _axis_ceiling(axis: dict, own: float, sentinel: float) -> tuple[float, bool]:
	"""The most of one axis a VM already on this host can occupy after a resize, and
	whether the axis is measured.

	A resize reshapes the VM in place, freeing its OWN current usage before re-reserving
	the new size — so the ceiling is the host's free room with that footprint added back:
	`effective - (used - own)`, i.e. `effective - used + own` (clamped at 0). Uncatalogued
	axis (`effective is None`) → the sentinel, flagged unmeasured."""
	if axis["effective"] is None:
		return sentinel, False
	return max(0.0, axis["effective"] - axis["used"] + own), True


def resize_headroom(vm: str) -> dict | None:
	"""The largest shape `vm` can resize to on the host it already occupies, or None when
	the VM (or its host) is unknown.

	Unlike `largest_vm` — the best *other* host's free headroom for a NEW machine — a
	resize stays on the VM's current host, so the ceiling is THAT host's free room with
	the VM's own footprint added back (`_axis_ceiling`). This guarantees the VM can always
	keep its size or shrink, and grow into whatever else the host has spare — so Central
	can offer only resize targets that will actually fit, instead of letting an oversized
	resize fail on the host. Returns `{vcpus, memory_megabytes, disk_gigabytes,
	unmeasured}`, matching `largest_vm`'s shape; an unreported axis contributes a sentinel
	and marks the shape `unmeasured`."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	row = frappe.db.get_value(
		"Virtual Machine",
		vm,
		["server", "vcpus", "cpu_max_cores", "memory_megabytes", "disk_gigabytes", "data_disk_gigabytes"],
		as_dict=True,
	)
	if not row or not row.server:
		return None

	c = capacity_for_server(row.server)
	own_cpu = float(row.cpu_max_cores or row.vcpus or 0)
	own_mem = float(row.memory_megabytes or 0)
	own_disk = float((row.disk_gigabytes or 0) + (row.data_disk_gigabytes or 0))
	cpu, m_cpu = _axis_ceiling(c["cpu"], own_cpu, _UNMEASURED_VCPUS)
	mem, m_mem = _axis_ceiling(c["memory"], own_mem, _UNMEASURED_MEMORY_MB)
	disk, m_disk = _axis_ceiling(c["disk"], own_disk, _UNMEASURED_DISK_GB)
	measured = m_cpu and m_mem and m_disk
	return {
		"vcpus": int(cpu),
		"memory_megabytes": int(mem),
		"disk_gigabytes": int(disk),
		"unmeasured": not measured,
	}


def _resize_fits(axis: dict, own: float, need: float) -> bool:
	"""Does a resized `need` fit on this axis, given the VM's own `own` footprint is freed
	first? The write-side predicate of `resize_headroom`'s ceiling (`effective - used +
	own`): an uncatalogued axis (`effective is None`) is unlimited → always fits, and the
	VM can always keep its current size or shrink (`need <= own <= ceiling`)."""
	ceiling, _ = _axis_ceiling(axis, own, float("inf"))
	return need <= ceiling


def ensure_resize_capacity(
	virtual_machine,
	*,
	new_cpu_cores: float,
	new_memory_mb: float,
	new_disk_gb: float,
) -> None:
	"""Authoritative capacity gate for an in-place resize — the write-side twin of
	`resize_headroom`, and the only capacity check on the resize path.

	Locks the VM's host and checks the new shape fits with the VM's OWN current footprint
	freed (a resize releases it before re-reserving), so a downsize or a same-size no-op
	always passes and a grow is capped to the host's real free room. Without this, resize
	has no gate at all: an oversized grow over-commits the host and fails at boot instead
	of failing fast here. The lock is the same `Server`-row lock create takes, so a resize
	and a create can't both consume the same free room. Raises NoCapacityError otherwise.

	No-op when the VM has no host yet (nothing placed to resize), mirroring
	`resize_headroom` returning None."""
	from atlas.atlas.api.server_capacity import capacity_for_server

	if not virtual_machine.server:
		return
	_lock_host(virtual_machine.server)
	c = capacity_for_server(virtual_machine.server)
	own_cpu = float(virtual_machine.cpu_max_cores or virtual_machine.vcpus or 0)
	own_mem = float(virtual_machine.memory_megabytes or 0)
	own_disk = float((virtual_machine.disk_gigabytes or 0) + (virtual_machine.data_disk_gigabytes or 0))
	if not (
		_resize_fits(c["cpu"], own_cpu, float(new_cpu_cores or 0))
		and _resize_fits(c["memory"], own_mem, float(new_memory_mb or 0))
		and _resize_fits(c["disk"], own_disk, float(new_disk_gb or 0))
	):
		frappe.throw(
			_("Not enough capacity on this host to resize to that size — contact your operator."),
			NoCapacityError,
		)


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
		required_vcpus = float(virtual_machine.cpu_max_cores or virtual_machine.vcpus or 1)
		# Memory and reserved disk (root + data), matching capacity_for_server's
		# per-axis used sums — the VM must fit on all three axes.
		required_memory = float(virtual_machine.memory_megabytes or 0)
		required_disk = float(
			(virtual_machine.disk_gigabytes or 0) + (virtual_machine.data_disk_gigabytes or 0)
		)
		virtual_machine.server = default_server(required_vcpus, required_memory, required_disk)


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
