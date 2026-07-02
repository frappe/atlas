"""VM migration orchestration — the resumable phase machine and its callback.

See spec/19-vm-migration.md. This is the CONTROLLER-side driver; the host work
runs in the `migration-*` Task scripts (scripts/). Wired in hooks.py:

    scheduler_events = {"cron": {"*/2 * * * *": ["atlas.atlas.migration.reconcile_migrations"]}}

The design point: a migration is a sequence of idempotent host phases, each
recorded as the Migration row's `status`. The scheduler re-enters the recorded
phase every tick — so a dropped RQ job, a provider rate-limit, an SSH blip, or a
worker crash never strands a migration. It resumes from the DB, never from
in-memory state.

**Stage 1 (this build): change-address only.** The VM keeps its UUID (and every
host-local value derived from it) but gets a NEW public IPv6 on the target, and
the proxy/Subdomain layer is re-pointed to it. The keep-address paths (Scaleway
range-move §2, DigitalOcean permanent-forward §2.9) are later stages.

**Transport (this build): plain TCP.** The source binds `qemu-nbd` to its public
IPv4 and the target's `nbd-client` dials it directly — no SSH tunnel yet (the
host-to-host credential is a deferred stage-3 prerequisite, spec/19 §2.1). This
data path is unencrypted; it is a deliberate get-it-working-first shortcut.

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

from atlas.atlas.networking import allocate_ipv6, derive_ipv4_link
from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result

# Phase order. The scheduler advances a row from one to the next; each name is
# also a key in PHASES below. Done/Failed are terminal (handled by the row).
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
	terminal failure marks only its own row Failed. Re-entrant by construction — if
	the previous tick crashed mid-phase, this tick re-enters the same phase
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
	cursor carried in. A phase returns True (advance) or False (re-enter next tick —
	the only non-advancing phase is Hydrating, which polls). Each phase first checks
	its resume key, so a re-entry after a crash is a cheap no-op up to where it got."""
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

	target = frappe.db.get_value("Server", target_server, ["status", "provider_type"], as_dict=True)
	if not target:
		frappe.throw(f"Target server {target_server} does not exist")
	if target.status != "Active":
		frappe.throw(f"Target server {target_server} is not Active (status is {target.status})")

	# Same provider: cross-provider migration is out of scope. The Server's own
	# frozen `provider_type` is the vendor (a real column, not a derived property).
	source_provider = frappe.db.get_value("Server", vm.server, "provider_type")
	if source_provider != target.provider_type:
		frappe.throw(
			"Cross-provider migration is out of scope (source and target must share a provider): "
			f"{source_provider} != {target.provider_type}"
		)
	# Region is same-by-construction: one region per Atlas instance (spec/19 §1),
	# and Subdomain has no region field. Nothing to compare.

	# IPv6 capacity on the target: allocate_ipv6 raises if the range is full. We
	# probe it here (read-only intent) so the operator learns at click time, not
	# three phases deep. The authoritative allocation is in InjectingIdentity.
	_assert_ipv6_capacity(target_server)

	if vm.public_ipv4 and not release_reserved_ip:
		frappe.throw(
			"This VM has an attached public IPv4 (Reserved IP) bound to the source host. "
			"Stage-1 migration cannot move it; pass release_reserved_ip=True to acknowledge "
			"inbound v4 will be released, then re-attach a target-server Reserved IP afterward."
		)


def _assert_ipv6_capacity(server: str) -> None:
	"""Probe-only. allocate_ipv6 holds the Server row for_update and would actually
	consume a slot, so we replicate its capacity question read-only: is there a free
	address in the range? The authoritative gate is allocate_ipv6() in
	InjectingIdentity; a race that fills the last slot between now and then is caught
	there and fails that migration cleanly."""
	network = ipaddress.IPv6Network(frappe.db.get_value("Server", server, "ipv6_virtual_machine_range"))
	used = {
		str(ipaddress.IPv6Address(address))
		for address in frappe.get_all(
			"Virtual Machine",
			filters={"server": server, "status": ["!=", "Terminated"]},
			pluck="ipv6_address",
		)
		if address
	}
	for index, candidate in enumerate(network.hosts()):
		if index < 1:  # ::1 is the host
			continue
		if str(candidate) not in used:
			return
	frappe.throw(f"Target server {server} has no free IPv6 address in its range")


# ─────────────────────────────────────────────────────────────────────────────
# Phases. Each returns True (advance) or False (re-enter next tick).
# ─────────────────────────────────────────────────────────────────────────────


def _phase_pending(doc) -> bool:
	"""Ensure the VM is Stopped with NO pending memory snapshot. A captured RAM image
	is worthless on the target (different host), so we always plain-stop and force a
	cold boot. Idempotent: a Stopped VM with has_memory_snapshot=0 is a no-op."""
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if vm.status in ("Running", "Paused"):
		# Plain stop (never snapshot-stop). flags.migrating exempts this internal
		# save from the lifecycle guard and (harmlessly) from the immutability gate.
		vm.flags.migrating = True
		vm.stop(memory_snapshot=False)
	if vm.has_memory_snapshot:
		vm.db_set("has_memory_snapshot", 0)
	return vm.status == "Stopped"


def _phase_exporting_snapshot(doc) -> bool:
	"""Source: thin-snap the disk(s) and start the NBD export bound to the source's
	public IPv4 (plain TCP — no tunnel this stage). Idempotent: the script re-uses an
	existing snapshot and an already-serving NBD process; we just re-record the port/pid."""
	task = _run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-export-source",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"NBD_PORT": str(nbd_port(doc.virtual_machine)),
			"BIND_ADDRESS": _server_ipv4(doc.source_server),
		},
		timeout_seconds=300,
	)
	result = parse_result(task.stdout)
	doc.db_set({"nbd_port": result["nbd_port"], "nbd_pid": result["nbd_pid"]})
	return True


def _phase_target_preparing(doc) -> bool:
	"""Target: pre-flight (modules/image/pool), create fresh thin LVs, connect the
	nbd client to the source over plain TCP, build the dm-clone device. Resume key:
	the script skips any step whose artifact already exists."""
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-clone-target",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"IMAGE_NAME": _vm_field(doc, "image"),
			"DISK_GB": str(_vm_field(doc, "disk_gigabytes")),
			"DATA_DISK_GB": str(_vm_field(doc, "data_disk_gigabytes") or 0),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(doc.nbd_port),
			"PHASE": "prepare",
		},
		timeout_seconds=600,
	)
	return True


def _phase_injecting_identity(doc) -> bool:
	"""Allocate the NEW /128 on the target and record it on the row. This is
	CONTROLLER-side only in stage 1: the disk is still hydrating (reads through NBD),
	so the actual identity inject + unit launch is deferred to CutoverStarting, where
	provision-vm runs against the collapsed disk with preserve_host_keys=1. Resume
	key: ipv6_address_new already set on the row.

	allocate_ipv6 holds the target Server row for_update — atomic, so two parallel
	migrations can't grab the same address. Persist before advancing so a crash
	re-uses the same address on re-entry (throws if the range filled since pre-flight)."""
	if not doc.ipv6_address_new:
		new_ipv6 = allocate_ipv6(doc.target_server)
		doc.db_set("ipv6_address_new", new_ipv6)
	return True


def _phase_hydrating(doc) -> bool:
	"""The ONLY non-advancing phase: enable hydration once, then poll. Returns False
	until 100% so the scheduler re-enters it each tick — a multi-minute copy becomes a
	series of cheap, read-only probes that never hold a worker. Stall guard: no
	progress for HYDRATION_STALL_TICKS → raise (→ Failed)."""
	task = _run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-poll-hydration",
		variables={"VIRTUAL_MACHINE_NAME": doc.virtual_machine},
		timeout_seconds=60,
	)
	result = parse_result(task.stdout)
	percent = int(result["hydration_percent"])
	stalled = percent == (doc.hydration_percent or 0)
	doc.db_set({"hydration_percent": percent, "hydration_last_polled": frappe.utils.now_datetime()})
	if percent >= 100:
		return True
	if stalled:
		ticks = (doc.hydration_stall_ticks or 0) + 1
		if ticks >= HYDRATION_STALL_TICKS:
			frappe.throw(f"hydration stalled at {percent}% for {ticks} ticks")
		doc.db_set("hydration_stall_ticks", ticks)
	else:
		doc.db_set("hydration_stall_ticks", 0)
	return False  # re-enter next tick


def _phase_cutover_starting(doc) -> bool:
	"""The cutover, in two host steps against the target:

	1. `migration-cutover-target` collapses the now-100%-hydrated dm-clone(s) to the
	   plain `atlas-vm-<uuid>` thin LV (idempotent: no-op if already collapsed), and
	   disconnects the nbd client. The disk is now pure-local.
	2. `provision-vm` (the proven launch path) runs against that existing disk — its
	   `snapshot_into`/`prepare_lv` no-ops since the LV exists, so it reuses the
	   hydrated bytes, injects the NEW identity with `preserve_host_keys=1` (SSH host
	   keys survive the move), builds the jail + launcher, and starts the unit. The
	   VM boots on the target's NEW /128.

	Resume key: both steps are idempotent, so a re-entry re-collapses (no-op) and
	re-provisions (reuses disk, re-launches unit) cleanly."""
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-cutover-target",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"DATA_DISK_GB": str(_vm_field(doc, "data_disk_gigabytes") or 0),
		},
		timeout_seconds=120,
	)
	# Launch on the target with the NEW address, reusing the hydrated disk. We build
	# the full provision variable set from the VM doc, then override the address
	# fields (the doc still points at the source /128 until Repointing) and set
	# preserve_host_keys so the moved SSH identity survives.
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	variables = vm._provision_variables()
	host_cidr, guest_cidr = derive_ipv4_link(doc.ipv6_address_new)
	variables.update(
		{
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_new,
			"IPV4_HOST_CIDR": host_cidr,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
			"PRESERVE_HOST_KEYS": "1",
		}
	)
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="provision-vm",
		variables=variables,
		timeout_seconds=120,
	)
	return True


def _phase_repointing(doc) -> bool:
	"""The point of no return — all Frappe-side. Commit the VM row to the target +
	new address, then re-point and reconcile every Subdomain. Idempotent: a second run
	sets the same values and reconciles the same (already-converged) map."""
	_finalize_cutover(doc)
	_repoint_routes(doc)
	_handle_reserved_ip(doc)
	return True


def _phase_cleanup(doc) -> bool:
	"""Source: kill NBD, lvremove the -migrate snapshots, tear down the stale source
	copy (old dir/LVs/netns). If it fails, the row stays at Cleanup with the error —
	there is no orphaned-LV reconciler, so the row IS the backstop."""
	_run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-cleanup-source",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"NBD_PORT": str(doc.nbd_port or 0),
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
	"""Rewrite every Subdomain's denormalized address to the new /128 via db_set (the
	field is read_only + only refreshed inside validate's _denormalize_address, so a
	plain save wouldn't change it predictably), then reconcile the whole proxy fleet
	(each proxy holds the whole map; there is no per-region push). Idempotent."""
	from atlas.atlas.proxy import reconcile_proxies

	changed = False
	for row in frappe.get_all(
		"Subdomain",
		filters={"virtual_machine": doc.virtual_machine},
		fields=["name", "address"],
	):
		if row.address != doc.ipv6_address_new:
			frappe.db.set_value("Subdomain", row.name, "address", doc.ipv6_address_new)
			changed = True
	if changed:
		# reconcile_proxies tolerates a wedged/empty fleet (per-proxy failure
		# isolation), so this never strands the migration.
		reconcile_proxies()


def _handle_reserved_ip(doc) -> None:
	"""Stage 1: detach any attached Reserved IP (it's bound to the source droplet and
	cannot follow the VM yet). The operator re-attaches a target-server Reserved IP
	afterward. Pre-flight already required the explicit release_reserved_ip ack, so
	this is not a surprise. (Reserved-IP preserve/reassign is a later stage — §6.)"""
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if not vm.public_ipv4:
		return
	for name in frappe.get_all("Reserved IP", filters={"virtual_machine": doc.virtual_machine}, pluck="name"):
		frappe.get_doc("Reserved IP", name).detach()


# ─────────────────────────────────────────────────────────────────────────────
# Task running + lost-task detection.
# ─────────────────────────────────────────────────────────────────────────────


def _run_phase_task(doc, *, server: str, script: str, variables: dict, timeout_seconds: int):
	"""Run a phase's host script inline. run_task saves the Task row first and raises
	on failure (→ caught by reconcile_migrations → Failed). Lost-task detection scans
	for a prior Running/Pending Task of the same script that blew its timeout and
	re-enters transparently (recorded, never a silent duplicate)."""
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
	its timeout, it's lost (the worker died mid-run). Log it and mark it Failure; the
	inline re-run that follows is safe because every phase script is idempotent. We
	record rather than heal silently — transparency over magic."""
	rows = frappe.get_all(
		"Task",
		filters={
			"virtual_machine": doc.virtual_machine,
			"script": script,
			"status": ["in", ("Running", "Pending")],
		},
		fields=["name", "creation"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return
	started = rows[0].creation
	if started and frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), started) > (
		LOST_TASK_TIMEOUT_FACTOR * timeout_seconds
	):
		frappe.logger("atlas").warning(
			f"migration {doc.name}: Task {rows[0].name} ({script}) appears lost; "
			f"re-entering phase idempotently"
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


def _vm_field(doc, field: str):
	return frappe.db.get_value("Virtual Machine", doc.virtual_machine, field)


def _server_ipv4(server: str) -> str:
	return frappe.db.get_value("Server", server, "ipv4_address")


def nbd_port(virtual_machine: str) -> int:
	"""A stable per-VM TCP port so concurrent migrations on one source host never
	collide. Derived like the other UUID-keyed values (tap/mac/uid)."""
	import uuid as _uuid

	return 10000 + (int(_uuid.UUID(virtual_machine).hex[:4], 16) % 5000)
