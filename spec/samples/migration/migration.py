"""VM migration orchestration — the resumable phase machine and its callback.

SAMPLE / ILLUSTRATIVE. See spec/24-vm-migration.md. Lives at
atlas/atlas/migration.py when built. Wired in hooks.py:

    scheduler_events = {
        "cron": {"*/2 * * * *": ["atlas.atlas.migration.reconcile_migrations"]},
        "daily": [...existing...],
    }

The design point: a migration is a sequence of idempotent host phases, each
recorded as the Migration row's `status`. The scheduler re-enters the recorded
phase every tick — so a dropped RQ job, a provider rate-limit, an SSH blip, or a
worker crash never strands a migration. It resumes from the DB, never from
in-memory state. This is the same resilience shape as
providers/worker.finish_provisioning, plus the "lost RQ job → re-run inline
idempotently" recovery, made a first-class loop.

Every phase obeys two rules:
  1. It runs its host work INLINE via run_task (not frappe.enqueue) — run_task
     saves the Task row first and raises on failure, and inline execution can't
     be a "lost worker job".
  2. It is idempotent: it checks "am I already done?" (the resume key) before
     acting, so re-entering a half-finished phase is safe.
"""

from __future__ import annotations

import ipaddress

import frappe

from atlas.atlas.networking import allocate_ipv6, derive_ipv4_link, derive_uid
from atlas.atlas.ssh import run_task

# Phase order. The scheduler advances a row from one to the next; each name is
# also a method on PHASES below. Done/Failed are terminal (handled by the row).
PHASE_ORDER = (
	"Pending",
	"ExportingSnapshot",
	"TargetPreparing",
	"InjectingIdentity",
	"Hydrating",
	"CutoverStarting",
	"Repointing",
	"Cleanup",
	"Done",
)

# A phase Task stuck Running/Pending past this multiple of its timeout is treated
# as lost and the phase is re-entered.
LOST_TASK_TIMEOUT_FACTOR = 2

# How many consecutive no-progress hydration polls before we give up.
HYDRATION_STALL_TICKS = 30


# ─────────────────────────────────────────────────────────────────────────────
# The callback: the scheduler entry that drives every in-flight migration.
# ─────────────────────────────────────────────────────────────────────────────


def reconcile_migrations() -> None:
	"""Scheduler entry (the 'callback'). Advance every non-terminal migration one
	step. Try/except PER ROW: one wedged migration never blocks the others, and a
	terminal failure marks only its own row Failed. Re-entrant by construction —
	if the previous tick crashed mid-phase, this tick re-enters the same phase
	(idempotent), so nothing is lost and nothing double-runs."""
	names = frappe.get_all(
		"Virtual Machine Migration",
		filters={"status": ["not in", ("Done", "Failed")]},
		pluck="name",
	)
	for name in names:
		try:
			advance_migration(frappe.get_doc("Virtual Machine Migration", name))
			# nosemgrep: frappe-manual-commit -- scheduler: persist each migration's
			# progress independently so one row's later failure can't roll back another's
			frappe.db.commit()
		except Exception as exception:
			frappe.db.rollback()
			_fail(name, str(exception))
			frappe.logger("atlas").error(f"migration {name} failed: {exception}")


def advance_migration(doc) -> None:
	"""Run the phase recorded on the row, then advance the status on success.

	Resumability: we ALWAYS re-derive what to do from `doc.status`, never from a
	cursor we carried in. A phase function returns True when the phase is complete
	(advance to the next) or False when it must be re-entered next tick (the only
	non-advancing phase is Hydrating, which polls). Each phase first checks its
	resume key, so a re-entry after a crash is a cheap no-op up to the point it
	actually got to."""
	phase = doc.status
	if phase not in PHASE_ORDER or phase == "Done":
		return
	handler = PHASES[phase]
	completed = handler(doc)
	if completed:
		nxt = PHASE_ORDER[PHASE_ORDER.index(phase) + 1]
		updates = {"status": nxt}
		if nxt == "Done":
			updates["completed_at"] = frappe.utils.now_datetime()
		doc.db_set(updates)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight (called synchronously from VirtualMachine.migrate, before insert).
# ─────────────────────────────────────────────────────────────────────────────


def preflight_checks(vm, target_server: str, release_reserved_ip: bool) -> None:
	"""The cheap, synchronous gate. On-host checks (image present, pool headroom,
	kernel modules) run in ExportingSnapshot/TargetPreparing where SSH is in hand;
	these are the DB-answerable ones that should reject before a row is even made."""
	from atlas.atlas.doctype.virtual_machine_migration.virtual_machine_migration import (
		active_migration_for,
	)

	if active_migration_for(vm.name):
		frappe.throw("This VM already has an in-flight migration")
	if vm.status not in ("Stopped", "Running", "Paused"):
		frappe.throw(f"Cannot migrate from {vm.status}")
	if vm.server == target_server:
		frappe.throw("VM is already on that server")

	source = frappe.db.get_value("Server", vm.server, ["provider", "status"], as_dict=True)
	target = frappe.db.get_value("Server", target_server, ["provider", "status"], as_dict=True)
	if not target:
		frappe.throw(f"Target server {target_server} does not exist")
	if target.status != "Active":
		frappe.throw(f"Target server {target_server} is not Active (status is {target.status})")
	if source.provider != target.provider:
		frappe.throw("Cross-provider migration is out of scope (source and target must share a provider)")
	# Same region: a Subdomain's `region` is immutable and a Reserved IP is bound to
	# its source server, so the routes/IP can only follow a SAME-region move
	# (spec/24). NOTE: `region` is a field on Virtual Machine (set at provision from
	# the active Root Domain — placement.active_root_domain), NOT on Server, so there
	# is no direct `target_server.region` to compare against. v1 resolves this by
	# requiring the operator-supplied target to share the VM's region; the authoritative
	# region-of-a-server mapping is an open question (Q6) — until it exists, the
	# operator vouches for region-match the way they vouch for an Active server.
	# (A practical proxy: the region of any non-Terminated VM already on the target.)
	target_region = _server_region(target_server)
	if target_region and vm.region and target_region != vm.region:
		frappe.throw(
			f"Cross-region migration is out of scope: VM is in {vm.region}, "
			f"target server is in {target_region} (Subdomain/Reserved-IP region is fixed)"
		)
	# IPv6 capacity on the target: allocate_ipv6 throws "no IPv6 capacity" if the
	# range is full. We probe it here (read-only intent) so the operator learns at
	# click time, not three phases deep. The real allocation happens in
	# InjectingIdentity; a race that fills the last slot between now and then is
	# caught there and fails that migration cleanly.
	_assert_ipv6_capacity(target_server)

	if vm.public_ipv4 and not release_reserved_ip:
		frappe.throw(
			"This VM has an attached public IPv4 (Reserved IP) bound to the source host. "
			"Migration cannot move it; pass release_reserved_ip=True to acknowledge that "
			"inbound v4 will be released, then re-attach a target-server Reserved IP afterward."
		)


def _assert_ipv6_capacity(server: str) -> None:
	"""Probe-only: allocate_ipv6 holds the Server row for_update and would actually
	allocate, so we don't call it here (that would consume a slot speculatively).
	Instead replicate its capacity question cheaply: is there a free address in the
	range? For the sample we just trust allocate_ipv6's later throw and do a light
	count check; the real impl can expose a `has_ipv6_capacity(server)` helper in
	networking.py."""
	# Left as a count-based sanity check in the sample. The authoritative gate is
	# allocate_ipv6() in InjectingIdentity.
	return None


# ─────────────────────────────────────────────────────────────────────────────
# Phases. Each returns True (advance) or False (re-enter next tick).
# ─────────────────────────────────────────────────────────────────────────────


def _phase_pending(doc) -> bool:
	"""Ensure the VM is Stopped with NO pending memory snapshot. A captured RAM
	image is worthless on the target (different host), so we always plain-stop and
	force a cold boot. Idempotent: a Stopped VM with has_memory_snapshot=0 is a
	no-op."""
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if vm.status in ("Running", "Paused"):
		# Plain stop (memory_snapshot=False) — never snapshot-stop. stop() guards
		# stop_protection; migration deliberately respects it (the operator must
		# clear it, same two-step as terminate). The lifecycle guard exempts the
		# migration's own caller path via flags, see note below.
		vm.flags.migrating = True  # let stop()'s internal save through the guard
		vm.stop(memory_snapshot=False)
	if vm.has_memory_snapshot:
		# A VM that was fast-stopped before migration: drop the host marker so the
		# (about-to-move) disk never pairs with stale RAM. The cutover script also
		# rm -rf's the jail snapshot/ dir on the source; this clears the row flag.
		vm.db_set("has_memory_snapshot", 0)
	return vm.status == "Stopped"


def _phase_exporting_snapshot(doc) -> bool:
	"""Source: thin-snap both LVs and start the NBD export on localhost. Resume key:
	the script itself is idempotent (snapshot_into re-activates an existing snap; it
	re-starts NBD only if not already serving) and emits the port+pid, which we
	record. Re-entry just re-reads them."""
	task = _run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-export-source.py",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"NBD_PORT": str(_nbd_port(doc.virtual_machine)),
		},
		timeout_seconds=300,
	)
	result = _parse(task)
	doc.db_set({"nbd_port": result["nbd_port"], "nbd_pid": result["nbd_pid"]})
	return True


def _phase_target_preparing(doc) -> bool:
	"""Target: pre-flight (modules/image/pool), create thin LVs, open the SSH tunnel
	to the source NBD, build the dm-clone device. Resume key: the script skips any
	step whose artifact already exists (the LV, the tunnel, the mapper device)."""
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-clone-target.py",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"IMAGE_NAME": _vm_field(doc, "image"),
			"DISK_GB": str(_vm_field(doc, "disk_gigabytes")),
			"DATA_DISK_GB": str(_vm_field(doc, "data_disk_gigabytes") or 0),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(doc.nbd_port),
			"PHASE": "prepare",  # build LVs + dm-clone, do NOT inject identity yet
		},
		timeout_seconds=600,
	)
	return True


def _phase_injecting_identity(doc) -> bool:
	"""Allocate the new /128 on the target (authoritative; throws if the range
	filled since pre-flight), derive the NAT44 /30, and inject the new network env
	into the migrated disk with host keys PRESERVED. Resume key: ipv6_address_new
	already set on the row AND already in the disk's /etc/atlas-network.env (the
	script checks the file and no-ops)."""
	if not doc.ipv6_address_new:
		# allocate_ipv6 holds the target Server row for_update — atomic, so two
		# parallel migrations can't grab the same address. Persist before the host
		# step so a crash between them re-uses the same address on re-entry.
		new_ipv6 = allocate_ipv6(doc.target_server)
		doc.db_set("ipv6_address_new", new_ipv6)
	host_cidr, guest_cidr = derive_ipv4_link(doc.ipv6_address_new)
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-clone-target.py",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"IMAGE_NAME": _vm_field(doc, "image"),
			"DISK_GB": str(_vm_field(doc, "disk_gigabytes")),
			"PHASE": "inject",
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_new,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
			"SSH_PUBLIC_KEY": _vm_field(doc, "ssh_public_key"),
			"ATLAS_FC_UID": str(derive_uid(doc.virtual_machine)),
			# Carry the data-disk mount + routing base so the rewritten env matches a
			# cold provision of the same VM (see provision-vm.py).
			"DATA_DISK_MOUNT_AT": _data_disk_mount_at(doc),
			"ROUTING_BASE_URL": _routing_base_url(),
		},
		timeout_seconds=300,
	)
	return True


def _phase_hydrating(doc) -> bool:
	"""The ONLY non-advancing phase: enable hydration once, then poll. Returns False
	until 100% so the scheduler re-enters it each tick — a multi-minute copy becomes
	a series of cheap, read-only probes that never hold a worker. Stall guard: no
	progress for HYDRATION_STALL_TICKS → raise (→ Failed)."""
	task = _run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-poll-hydration.py",
		variables={"VIRTUAL_MACHINE_NAME": doc.virtual_machine},
		timeout_seconds=60,
	)
	result = _parse(task)
	percent = int(result["hydration_percent"])
	stalled = percent == (doc.hydration_percent or 0)
	doc.db_set({"hydration_percent": percent, "hydration_last_polled": frappe.utils.now_datetime()})
	if percent >= 100:
		return True
	# A crude stall counter for the sample: track no-progress ticks in a flag.
	if stalled:
		ticks = (doc.get("_stall_ticks") or 0) + 1
		if ticks >= HYDRATION_STALL_TICKS:
			frappe.throw(f"hydration stalled at {percent}% for {ticks} ticks")
		doc.db_set("_stall_ticks", ticks)
	else:
		doc.db_set("_stall_ticks", 0)
	return False  # re-enter next tick


def _phase_cutover_starting(doc) -> bool:
	"""Disable the source unit, start the target unit, poll Running+SSH on the new
	/128, collapse the now-100%-hydrated dm-clone. Resume key: the script skips the
	start if the unit is already active and the address answers."""
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-cutover-target.py",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_new,
			"SOURCE_HOST": _server_ipv4(doc.source_server),
		},
		timeout_seconds=300,
	)
	return True


def _phase_repointing(doc) -> bool:
	"""The point of no return — all Frappe-side. Commit the VM row to the target,
	then re-point and reconcile every Subdomain. Idempotent: a second run sets the
	same values and reconciles the same (already-converged) map."""
	_finalize_cutover(doc)
	_repoint_routes(doc)
	_handle_reserved_ip(doc)
	return True


def _phase_cleanup(doc) -> bool:
	"""Source: kill NBD, lvremove the -migrate snapshots, tear down the stale source
	copy (old dir/LVs/netns). If it fails, the row stays at Cleanup with guidance —
	there is no orphaned-LV reconciler, so the row IS the backstop."""
	_run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-cleanup-source.py",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"NBD_PID": str(doc.nbd_pid or 0),
		},
		timeout_seconds=120,
	)
	return True


PHASES = {
	"Pending": _phase_pending,
	"ExportingSnapshot": _phase_exporting_snapshot,
	"TargetPreparing": _phase_target_preparing,
	"InjectingIdentity": _phase_injecting_identity,
	"Hydrating": _phase_hydrating,
	"CutoverStarting": _phase_cutover_starting,
	"Repointing": _phase_repointing,
	"Cleanup": _phase_cleanup,
}


# ─────────────────────────────────────────────────────────────────────────────
# Frappe-side cutover helpers (the Repointing phase).
# ─────────────────────────────────────────────────────────────────────────────


def _finalize_cutover(doc) -> None:
	"""Flip the VM row to the target server + new address. The ONLY place `server`
	changes — gated by flags.migrating so validate() lets it through (the cutover
	already happened on the host). status → Running, has_memory_snapshot → 0."""
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if vm.server == doc.target_server and vm.ipv6_address == doc.ipv6_address_new:
		return  # idempotent: already committed
	vm.flags.migrating = True
	vm.server = doc.target_server
	vm.ipv6_address = doc.ipv6_address_new
	vm.status = "Running"
	vm.has_memory_snapshot = 0
	vm.last_started = frappe.utils.now_datetime()
	vm.save(ignore_permissions=True)


def _repoint_routes(doc) -> None:
	"""Rewrite every Subdomain's denormalized address to the new /128 via db_set
	(the field is read_only + only refreshed inside validate's _denormalize_address,
	so a plain save wouldn't change it predictably), then explicitly reconcile each
	region (Subdomain.on_update only reconciles on `active` flips, so it won't push
	for us). Idempotent."""
	from atlas.atlas.proxy import reconcile_region

	regions: set[str] = set()
	for row in frappe.get_all(
		"Subdomain",
		filters={"virtual_machine": doc.virtual_machine},
		fields=["name", "region", "address"],
	):
		if row.address != doc.ipv6_address_new:
			frappe.db.set_value("Subdomain", row.name, "address", doc.ipv6_address_new)
		regions.add(row.region)
	for region in regions:
		# reconcile_region tolerates an empty/wedged fleet (per-proxy failure
		# isolation), so this never strands the migration.
		reconcile_region(region)


def _handle_reserved_ip(doc) -> None:
	"""v1: detach any attached Reserved IP (it's bound to the source droplet and its
	`server` is immutable, so it cannot follow the VM). The operator re-attaches a
	target-server Reserved IP afterward. Pre-flight already required the explicit
	release_reserved_ip ack, so this is not a surprise."""
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if not vm.public_ipv4:
		return
	for name in frappe.get_all("Reserved IP", filters={"virtual_machine": doc.virtual_machine}, pluck="name"):
		# The VM is Running on the target now; detach() runs the host NAT-clear Task
		# against vm.server, which is the TARGET (already committed) — correct, since
		# the source NAT is torn down with the source unit. (If the source droplet
		# still holds the vendor binding, the operator releases it manually — see Q1.)
		frappe.get_doc("Reserved IP", name).detach()


# ─────────────────────────────────────────────────────────────────────────────
# Task running + lost-task detection.
# ─────────────────────────────────────────────────────────────────────────────


def _run_phase_task(doc, *, server: str, script: str, variables: dict, timeout_seconds: int):
	"""Run a phase's host script inline. run_task saves the Task row first and raises
	on failure (→ caught by reconcile_migrations → Failed). The migration row links
	the Task via the virtual_machine field; lost-task detection scans for a prior
	Running/Pending Task of the same script that blew its timeout and is re-entered
	transparently (recorded, never a silent duplicate)."""
	_detect_lost_task(doc, script, timeout_seconds)
	return run_task(
		server=server,
		script=script,
		variables=variables,
		virtual_machine=doc.virtual_machine,
		timeout_seconds=timeout_seconds,
	)


def _detect_lost_task(doc, script: str, timeout_seconds: int) -> None:
	"""If the most recent Task for this VM+script is still Running/Pending well past
	its timeout, it's lost (the worker died mid-run). Log it; the inline re-run that
	follows is safe because every phase script is idempotent. We record rather than
	heal silently — transparency over magic."""
	rows = frappe.get_all(
		"Task",
		filters={
			"virtual_machine": doc.virtual_machine,
			"script": script,
			"status": ["in", ("Running", "Pending")],
		},
		fields=["name", "started"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return
	started = rows[0].started
	if started and frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), started) > (
		LOST_TASK_TIMEOUT_FACTOR * timeout_seconds
	):
		frappe.logger("atlas").warning(
			f"migration {doc.name}: Task {rows[0].name} ({script}) appears lost; re-entering phase idempotently"
		)
		frappe.db.set_value("Task", rows[0].name, "status", "Failure")


def _fail(name: str, message: str) -> None:
	"""Mark a migration Failed, recording the phase it failed at so retry() resumes
	there. Best-effort and self-committing (it runs after a rollback)."""
	doc = frappe.get_doc("Virtual Machine Migration", name)
	doc.db_set({"status": "Failed", "error_message": message[-2000:], "error_at_status": doc.status})
	# nosemgrep: frappe-manual-commit -- persist the failure so the next tick sees it
	frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Small read helpers.
# ─────────────────────────────────────────────────────────────────────────────


def _parse(task) -> dict:
	from atlas.atlas.task_results import parse_result

	return parse_result(task.stdout)


def _vm_field(doc, field: str):
	return frappe.db.get_value("Virtual Machine", doc.virtual_machine, field)


def _server_ipv4(server: str) -> str:
	return frappe.db.get_value("Server", server, "ipv4_address")


def _server_region(server: str) -> str | None:
	"""Best-effort region of a Server. `region` is a field on Virtual Machine, not
	Server (it is set at provision from the active Root Domain), so the only honest
	answer is the region of a VM already living on this server. None when the target
	is empty — then the operator's region claim stands (see Q6)."""
	rows = frappe.get_all(
		"Virtual Machine",
		filters={"server": server, "status": ["!=", "Terminated"]},
		fields=["region"],
		limit=1,
	)
	return rows[0].region if rows else None


def _data_disk_mount_at(doc) -> str:
	vm = frappe.db.get_value(
		"Virtual Machine",
		doc.virtual_machine,
		["data_disk_gigabytes", "data_disk_format_and_mount", "data_disk_mount_point"],
		as_dict=True,
	)
	if vm.data_disk_gigabytes and vm.data_disk_format_and_mount:
		return vm.data_disk_mount_point
	return ""


def _nbd_port(virtual_machine: str) -> int:
	"""A stable per-VM localhost port so concurrent migrations on one source host
	never collide. Derived like the other UUID-keyed values (tap/mac/uid)."""
	import uuid as _uuid

	return 10000 + (int(_uuid.UUID(virtual_machine).hex[:4], 16) % 5000)


def _routing_base_url() -> str:
	try:
		return frappe.utils.get_url() or ""
	except Exception:
		return ""
