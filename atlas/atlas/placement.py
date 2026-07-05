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


def image_home_servers(image: str) -> set[str]:
	"""The Active servers that actually hold this image's bytes.

	A `Virtual Machine Image` is ONE fleet-wide row, but its bytes are per-server:
	the row records nothing about where it landed. Presence is reconstructed from the
	Task/export trail — the same authoritative sources the image form already reads:

	- A **URL image** (`is_local` false) is downloadable; `after_insert` fans out a
	  `sync-image` Task to every Active server. Its home set is the servers where that
	  sync SUCCEEDED — the verifiable presence signal, matching
	  `VirtualMachineImage.sync_status`. (An enqueued-but-unfinished sync doesn't count;
	  the bytes aren't there yet.)
	- A **local image** (promoted from a snapshot, no rootfs URL) lives only where it
	  was promoted plus wherever it was later exported: the promote home
	  (`_image_home_server`) UNION every successful `Virtual Machine Image Export`'s
	  `target_server`. This is the presence a `sync-image` could never provide — a local
	  image is non-syncable.

	Result is intersected with the currently-Active servers: a home that has since been
	removed/drained can't take a VM, so it isn't a placement candidate. Returns a set
	(possibly empty — the caller decides whether that's a hard error)."""
	active = set(
		frappe.get_all(
			"Server",
			filters={"status": "Active"},
			pluck="name",
			ignore_permissions=True,
		)
	)
	if not active:
		return set()

	is_local = not (frappe.db.get_value("Virtual Machine Image", image, "rootfs_url") or "").strip()
	if is_local:
		homes = _local_image_home_servers(image)
	else:
		homes = _synced_image_home_servers(image)
	return homes & active


def _synced_image_home_servers(image: str) -> set[str]:
	"""Servers with a successful `sync-image` Task for this URL image. Matches
	`VirtualMachineImage.sync_status`: the immutable Task history is the presence trail,
	and the script verb was renamed (`sync-image`) from its legacy filenames, so we
	accept all three."""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT server FROM `tabTask`
		WHERE script IN ('sync-image', 'sync-image.py', 'sync-image.sh')
		  AND status = 'Success'
		  AND variables LIKE %(pattern)s
		""",
		{"pattern": f'%"IMAGE_NAME": "{image}"%'},
		pluck="server",
	)
	return {row for row in rows if row}


def _local_image_home_servers(image: str) -> set[str]:
	"""Servers holding a local (snapshot-promoted) image: its promote home plus every
	server a successful export shipped it to. The promote home comes from the same Task
	trail `Virtual Machine Image Export.before_insert` denormalizes from; the export
	targets come from the export rows themselves (their `target_server` is the durable
	record that the bytes landed there)."""
	from atlas.atlas.doctype.virtual_machine_image_export.virtual_machine_image_export import (
		_image_home_server,
	)

	homes: set[str] = set()
	promote_home = _image_home_server(image)
	if promote_home:
		homes.add(promote_home)
	# A Done export means the base LV + kernel reached target_server and the Registering
	# phase asserted the row; anything short of Done hasn't finished shipping the bytes.
	homes.update(
		frappe.get_all(
			"Virtual Machine Image Export",
			filters={"image": image, "status": "Done"},
			pluck="target_server",
		)
	)
	return {home for home in homes if home}


def default_server_for_image(
	image: str,
	required_vcpus: float,
	required_memory_mb: float,
	required_disk_gb: float,
) -> str:
	"""Like `default_server`, but only among servers that HOLD `image`.

	`default_server` picks the first Active host with room on all three axes — but a
	VM can only boot an image whose bytes are on its host. For a local (per-server)
	image, "any Active host" is wrong: it would pick a host missing the LV and the
	provision would fail on the box, not at the boundary. So we restrict the candidate
	pool to `image_home_servers(image)` and pick the first of those with capacity.

	Raises loudly (Taste 17) when the image is nowhere yet — "export it to a server
	first" — distinct from `NoCapacityError` (which means the image is present but no
	host that holds it has room)."""
	homes = image_home_servers(image)
	if not homes:
		frappe.throw(
			_(
				"Image {0} is not present on any active server yet — export it to a "
				"server before provisioning from it."
			).format(image)
		)
	return default_server(
		required_vcpus,
		required_memory_mb,
		required_disk_gb,
		candidate_servers=homes,
	)


def _fits(axis: dict, need: float) -> bool:
	"""Does `need` more of this resource fit on this axis?

	`effective is None` means the host is uncatalogued on this axis → unlimited
	room (the operator vouched for it by marking it Active), so anything fits.
	Otherwise the axis fits when used + need stays within the effective budget."""
	return axis["effective"] is None or axis["used"] + need <= axis["effective"]


def default_server(
	required_vcpus: float,
	required_memory_mb: float,
	required_disk_gb: float,
	candidate_servers: set[str] | None = None,
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

	`candidate_servers`, when given, restricts the pool to that set (still ordered
	by creation, still Active) — `default_server_for_image` passes the servers that
	hold the image so placement never picks a host missing its bytes. None means the
	whole Active fleet, the original behaviour.

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
	if candidate_servers is not None:
		servers = [server for server in servers if server in candidate_servers]
	if not servers:
		frappe.throw(_("No capacity available — contact your operator."), NoCapacityError)
	for server in servers:
		capacity = capacity_for_server(server)
		if (
			_fits(capacity["cpu"], required_vcpus)
			and _fits(capacity["memory"], required_memory_mb)
			and _fits(capacity["disk"], required_disk_gb)
		):
			return server
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
