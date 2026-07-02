"""VM migration orchestration — the resumable phase machine and its callback.

See spec/19-vm-migration.md. This is the CONTROLLER-side driver; the host work
runs in the `migration-*` Task scripts (scripts/). Wired in hooks.py:

    scheduler_events = {"cron": {"*/2 * * * *": ["atlas.atlas.migration.reconcile_migrations"]}}

The design point: a migration is a sequence of idempotent host phases, each
recorded as the Migration row's `status`. Two things drive it: `start_migration`
(enqueued by VirtualMachine.migrate on insert) runs the phases back-to-back, each
completed phase chaining the next so there is no wait between them; the
`reconcile_migrations` cron re-drives the one holding phase (Hydrating) and is the
safety net — a dropped RQ job, a provider rate-limit, an SSH blip, or a worker
crash never strands a migration, because the cron re-enters the recorded phase and
every phase resumes from the DB, never from in-memory state.

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

from atlas.atlas.networking import (
	allocate_ipv6,
	derive_ipv4_link,
	derive_vm_tunnel,
	derive_vm_tunnel_port,
	derive_vm_tunnel_table,
)
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
		_reconcile_one(name)


def start_migration(name: str) -> None:
	"""Background entrypoint: advance a migration one phase, then — if that phase
	completed and there is more to do immediately — enqueue itself again to run the
	NEXT phase right away. This is the "run actions one after another as soon as they
	can" driver: VirtualMachine.migrate enqueues the first call on insert, and each
	completed phase chains the next, so a migration walks Pending → … → Hydrating
	back-to-back with no wait for a cron tick between phases.

	It stops re-enqueuing when a phase HOLDS (Hydrating, which polls a multi-minute
	copy) or the migration reaches a terminal phase — from there the
	`reconcile_migrations` cron re-drives Hydrating each tick and remains the sole
	safety net if any of these jobs is dropped. Re-entrant and idempotent like the
	cron: it reloads and advances whatever phase the row records."""
	if not frappe.db.exists("Virtual Machine Migration", name):
		return
	if _reconcile_one(name):
		frappe.enqueue(
			"atlas.atlas.migration.start_migration",
			queue="long",
			timeout=300,
			name=name,
		)


def _reconcile_one(name: str) -> bool:
	"""Advance one migration a single phase, committing its progress on success and
	marking it Failed on error — in isolation, so one wedged row never blocks or
	rolls back another. Shared by the cron and the on-insert kick. Returns True iff
	the row advanced to a further non-terminal phase (more work to run immediately)."""
	try:
		advanced = advance_migration(frappe.get_doc("Virtual Machine Migration", name))
		# nosemgrep: frappe-manual-commit -- persist each migration's progress
		# independently so one row's later failure can't roll back another's
		frappe.db.commit()
		return advanced
	except Exception as exception:
		frappe.db.rollback()
		_fail(name, str(exception))
		frappe.logger("atlas").error(f"migration {name} failed: {exception}")
		return False


def advance_migration(doc) -> bool:
	"""Run the phase recorded on the row, then advance the status on success. Returns
	True iff the row advanced to a further NON-terminal phase — i.e. there is more
	work to run immediately (the caller should drive the next phase now rather than
	wait for a tick). Returns False when the phase held (Hydrating polling) or reached
	a terminal phase (Done).

	Resumability: we ALWAYS re-derive what to do from `doc.status`, never from a
	cursor carried in. A phase returns True (advance) or False (re-enter next tick —
	the only non-advancing phase is Hydrating, which polls). Each phase first checks
	its resume key, so a re-entry after a crash is a cheap no-op up to where it got."""
	phase = doc.status
	if phase not in PHASE_ORDER or phase == "Done":
		return False
	# Stamp the live progress line BEFORE running the phase, so the form shows what
	# the migration is doing the moment work starts — not only after the (possibly
	# multi-minute) host task returns. Phases that poll (Hydrating) and long
	# sub-steps (base-image ship) refine this line + progress_percent as they run.
	_progress(doc, _phase_label(doc, phase), percent=-1)
	handler = PHASES[phase]
	completed = handler(doc)
	if not completed:
		return False
	nxt = PHASE_ORDER[PHASE_ORDER.index(phase) + 1]
	updates = {"status": nxt, "progress_percent": -1}
	if nxt == "Done":
		updates["completed_at"] = frappe.utils.now_datetime()
		updates["progress_detail"] = "Migration complete."
	doc.db_set(updates)
	return nxt != "Done"


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
	# SKIPPED on the keep-address path — the VM keeps its /128 (the source forwards
	# it), so no address is allocated on the target and its range fullness is
	# irrelevant. The gate mirrors before_insert's _decide_address_scheme so the
	# operator learns the scheme's implications at click time.
	if not _will_keep_address(vm.server, target_server):
		_assert_ipv6_capacity(target_server)

	if vm.public_ipv4 and not release_reserved_ip:
		frappe.throw(
			"This VM has an attached public IPv4 (Reserved IP) bound to the source host. "
			"Stage-1 migration cannot move it; pass release_reserved_ip=True to acknowledge "
			"inbound v4 will be released, then re-attach a target-server Reserved IP afterward."
		)


def _will_keep_address(source_server: str, target_server: str) -> bool:
	"""Whether a migration between these two servers keeps the VM's /128 (spec/19
	§2.8). True iff BOTH hosts' provider can forward a /128 from the source
	(vm_range_is_forwardable). The single source of truth for the address scheme,
	shared by pre-flight (to skip the target-capacity check) and the Migration row's
	before_insert (to set keep_address/forward_address)."""
	from atlas.atlas.providers import for_provider_type

	provider_type = frappe.db.get_value("Server", source_server, "provider_type")
	provider = for_provider_type(provider_type)
	source_resource = frappe.db.get_value("Server", source_server, "provider_resource_id")
	target_resource = frappe.db.get_value("Server", target_server, "provider_resource_id")
	return bool(
		provider.vm_range_is_forwardable(source_resource)
		and provider.vm_range_is_forwardable(target_resource)
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
	# Record the source disks' ACTUAL byte sizes. The target disk must be created at
	# least this large — a VM disk that was lvextended past its doc `disk_gigabytes`
	# (e.g. a disk born as a CoW of a larger base image) is physically bigger than
	# the doc says, and sizing the target off the doc would truncate the filesystem
	# during hydration (dead superblock at cutover). See _target_disk_gb.
	doc.db_set(
		{
			"nbd_port": result["nbd_port"],
			"nbd_pid": result["nbd_pid"],
			"root_disk_bytes": int(result["root_size_bytes"]),
			"data_disk_bytes": int(result.get("data_size_bytes", 0)),
		}
	)
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
			# Size the target disk off the SOURCE's actual bytes, not the VM doc — a
			# grown disk is physically larger than disk_gigabytes, and under-sizing
			# truncates the filesystem during hydration (dead superblock at cutover).
			"DISK_GB": str(_target_disk_gb(doc, "disk_gigabytes", doc.root_disk_bytes)),
			"DATA_DISK_GB": str(_target_disk_gb(doc, "data_disk_gigabytes", doc.data_disk_bytes)),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(doc.nbd_port),
			"PHASE": "prepare",
		},
		timeout_seconds=600,
	)
	if doc.keep_address:
		_bring_up_forward_tunnel(doc)
	return True


def _bring_up_forward_tunnel(doc) -> None:
	"""keep-address only: create the per-VM forward tunnel on BOTH hosts (spec/19
	§2.9.1). Source first (the TCP listener), then target (the connector). The
	device name/port are pure functions of the UUID, so both ends agree with no
	shared state. Record the device name on the row (teardown/re-entry handle) and
	move tunnel_status to Armed — the routes that make traffic flow come at cutover.
	Idempotent: migration-forward-up no-ops on an already-live socat."""
	tunnel_device = derive_vm_tunnel(doc.virtual_machine)
	tunnel_port = derive_vm_tunnel_port(doc.virtual_machine)
	_run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-forward-up",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"ROLE": "source",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
		},
		timeout_seconds=60,
	)
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-forward-up",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"ROLE": "target",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
		},
		timeout_seconds=60,
	)
	doc.db_set({"tunnel_device": tunnel_device, "tunnel_status": "Armed"})


def _phase_injecting_identity(doc) -> bool:
	"""Decide the address the VM will boot with on the target and record it on the
	row. The actual identity inject + unit launch is deferred to CutoverStarting,
	where provision-vm runs against the collapsed disk with preserve_host_keys=1.
	Resume key: ipv6_address_new already set on the row.

	- change-address: allocate a NEW /128 from the target's range. allocate_ipv6
	  holds the target Server row for_update — atomic, so two parallel migrations
	  can't grab the same address. Persist before advancing so a crash re-uses the
	  same address on re-entry (throws if the range filled since pre-flight).
	- keep-address: NEAR-NO-OP for networking (spec/19 §2.9.4). The /128 is
	  unchanged — the source keeps holding the /64 and forwards it — so there is NO
	  allocate and NO env rewrite; the VM boots on the SAME address. We record the
	  unchanged address as ipv6_address_new so the shared cutover path (which
	  provisions against ipv6_address_new) launches it on the right /128."""
	if not doc.ipv6_address_new:
		address = doc.ipv6_address_old if doc.keep_address else allocate_ipv6(doc.target_server)
		doc.db_set("ipv6_address_new", address)
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
	_progress(
		doc,
		f"Copying disk blocks from {_server_title(doc.source_server)} to "
		f"{_server_title(doc.target_server)} — {percent}% hydrated.",
		percent=percent,
	)
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
	if doc.keep_address:
		_install_forward_routes(doc)
	return True


def _install_forward_routes(doc) -> None:
	"""keep-address only: now that the target VM is up on the SAME /128, wire the
	traffic path (spec/19 §2.2-2.3, §2.9.2-2.9.3). Target return-route FIRST (so the
	guest's replies have somewhere to go the instant inbound starts arriving), then
	the source forward (which points the /128 delivery at the tunnel and — on a
	proxy-NDP provider — re-asserts the NDP entry the source unit's stop removed).
	Idempotent: both scripts re-assert with `replace`/duplicate-guarded adds. Moves
	tunnel_status to Forwarding — the path is now live and stays up permanently."""
	tunnel_device = doc.tunnel_device or derive_vm_tunnel(doc.virtual_machine)
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-target-receive",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_old,
			"TUNNEL_DEVICE": tunnel_device,
			"ROUTE_TABLE": str(derive_vm_tunnel_table(doc.virtual_machine)),
		},
		timeout_seconds=60,
	)
	_run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-source-forward",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_old,
			"TUNNEL_DEVICE": tunnel_device,
			"REASSERT_PROXY_NDP": "1" if doc.forward_address else "0",
		},
		timeout_seconds=60,
	)
	doc.db_set({"tunnel_status": "Forwarding", "forward_active": 1})


def _phase_repointing(doc) -> bool:
	"""The point of no return — all Frappe-side. Commit the VM row to the target
	(and, change-address only, the new address), then re-point every Subdomain.
	Idempotent: a second run sets the same values and reconciles the same
	(already-converged) map.

	keep-address (spec/19 §2.9.4): the /128 never changed, so the Subdomain rows are
	already correct and the proxy already dials the right address — the SUBDOMAIN
	RE-POINT AND RECONCILE ARE SKIPPED ENTIRELY. `server` still flips (the VM really
	is on the target now); the address is copied verbatim."""
	_finalize_cutover(doc)
	if not doc.keep_address:
		_repoint_routes(doc)
	_handle_reserved_ip(doc)
	return True


def _phase_cleanup(doc) -> bool:
	"""Source: kill NBD, lvremove the -migrate snapshots, tear down the stale source
	copy (old dir/LVs/netns). If it fails, the row stays at Cleanup with the error —
	there is no orphaned-LV reconciler, so the row IS the backstop.

	keep-address (spec/19 §2.9.4): the SAME source teardown runs (the stale disk copy
	is gone either way), BUT the forward tunnel + source-forward route/nft + (DO)
	proxy-NDP + target return-rule are LEFT IN PLACE — they carry the VM's live
	traffic permanently. cleanup-source only removes the migration's transient
	snapshot/NBD state, not the tunnel, so nothing extra is needed to keep the
	forward up; we just record it on the VM so the cross-host dependency is visible
	and the operator can collapse it later (§2.9.5)."""
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
	if doc.keep_address:
		_record_forward_on_vm(doc)
	return True


def _record_forward_on_vm(doc) -> None:
	"""keep-address only: mark the migrated VM as having its traffic forwarded from
	the source host (spec/19 §2.9.5). Drives the VM-form dashboard indicator and
	gates the Collapse-forward action. Idempotent: re-recording the same source is a
	no-op. `since` is stamped only on the first record so a re-entry doesn't reset
	the clock. Uses db_set (bypasses the VM's immutability gate cleanly — these are
	read-only observability fields, not resource fields)."""
	if _vm_field(doc, "traffic_forwarded_from") == doc.source_server:
		return
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	vm.db_set(
		{
			"traffic_forwarded_from": doc.source_server,
			"traffic_forwarded_since": frappe.utils.now_datetime(),
		}
	)


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
# Collapse-forward: the operator-initiated teardown of a keep-address forward.
# ─────────────────────────────────────────────────────────────────────────────


def collapse_forward(vm) -> None:
	"""Tear down a VM's keep-address forward and fall it back to change-address
	(spec/19 §2.9.5). The forward is permanent by default; this is the ONLY point at
	which a kept address can still change, and it is entirely operator-initiated
	(via the VM-form Collapse-forward button). Steps, in order:

	  1. Tear the tunnel down on BOTH hosts — the target's return-rule + table, then
	     the source's route/nft/(DO)proxy-NDP, then the tunnel device + socat.
	  2. Allocate a NEW /128 from the CURRENT (post-migration) server's range and
	     re-provision the VM in place to inject it, preserving host keys — the same
	     shape a change-address cutover uses, but the disk is already local so
	     provision-vm just rewrites network.env + relaunches the unit on the new /128.
	  3. Re-point every Subdomain to the new /128 and reconcile the proxy fleet.
	  4. Clear the VM's forward markers.

	Idempotent enough to retry: a re-invoked collapse re-runs best-effort teardown
	(the down scripts tolerate missing state), and step 2's allocate is skipped once
	the VM already sits on a fresh in-range /128. The source host is the VM's
	traffic_forwarded_from; the current host is vm.server."""
	source_server = vm.traffic_forwarded_from
	if not source_server:
		frappe.throw(f"Virtual Machine {vm.name} has no active forward to collapse")

	tunnel_device = derive_vm_tunnel(vm.name)
	tunnel_port = derive_vm_tunnel_port(vm.name)
	route_table = derive_vm_tunnel_table(vm.name)
	forward_address = frappe.db.get_value("Server", source_server, "provider_type") == "DigitalOcean"
	old_ipv6 = vm.ipv6_address

	# 1a. Target end (the VM's current host): remove the return-route policy.
	run_task(
		server=vm.server,
		script="migration-forward-down",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"VIRTUAL_MACHINE_IPV6": old_ipv6,
			"ROLE": "target",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
			"ROUTE_TABLE": str(route_table),
		},
		virtual_machine=vm.name,
		timeout_seconds=60,
	)
	# 1b. Source end: remove the /128 route, nft rules, and (DO) proxy-NDP entry.
	run_task(
		server=source_server,
		script="migration-forward-down",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"VIRTUAL_MACHINE_IPV6": old_ipv6,
			"ROLE": "source",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
			"DEASSERT_PROXY_NDP": "1" if forward_address else "0",
		},
		virtual_machine=vm.name,
		timeout_seconds=60,
	)

	# 2. Allocate a fresh /128 on the current host and re-inject it in place. Skip
	#    the allocate if a prior collapse attempt already moved the VM off old_ipv6.
	new_ipv6 = vm.ipv6_address
	if new_ipv6 == old_ipv6:
		new_ipv6 = allocate_ipv6(vm.server)
	variables = vm._provision_variables()
	host_cidr, guest_cidr = derive_ipv4_link(new_ipv6)
	variables.update(
		{
			"VIRTUAL_MACHINE_IPV6": new_ipv6,
			"IPV4_HOST_CIDR": host_cidr,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
			"PRESERVE_HOST_KEYS": "1",
		}
	)
	run_task(
		server=vm.server,
		script="provision-vm",
		variables=variables,
		virtual_machine=vm.name,
		timeout_seconds=120,
	)

	# 3. Commit the new address on the VM row, clear the forward markers, then
	#    re-point the Subdomains at it (the change-address path — now the address
	#    really did change). db_set the address under flags so validate() is happy.
	vm.flags.migrating = True
	vm.ipv6_address = new_ipv6
	vm.traffic_forwarded_from = None
	vm.traffic_forwarded_since = None
	vm.save(ignore_permissions=True)
	_repoint_routes(_ForwardCollapse(vm.name, new_ipv6))


class _ForwardCollapse:
	"""A tiny duck-typed stand-in so collapse_forward can reuse _repoint_routes
	(which reads .virtual_machine and .ipv6_address_new off a migration row). The
	collapse is not a migration row, but the re-point logic is identical."""

	def __init__(self, virtual_machine: str, ipv6_address_new: str) -> None:
		self.virtual_machine = virtual_machine
		self.ipv6_address_new = ipv6_address_new


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


def _bytes_to_gib_ceil(size_bytes: int) -> int:
	"""Round a byte size UP to whole GiB — the target base LV must be at least the
	source's size (a smaller thin LV would truncate the copy)."""
	gib = 1024**3
	return (size_bytes + gib - 1) // gib


def _target_disk_gb(doc, doc_field: str, source_bytes) -> int:
	"""The size (whole GiB) to create a migrated disk at on the target: the MAX of
	the VM doc's declared size and the source disk's ACTUAL bytes. A disk that was
	lvextended past its doc size (or born as a CoW of a larger base image) is
	physically bigger than `disk_gigabytes`; hydrating its full block count into a
	doc-sized (smaller) LV truncates the filesystem and leaves an unreadable
	superblock at cutover. Never under-size; growing to match is safe. Returns 0 for
	an absent data disk (source_bytes 0 and doc field 0)."""
	declared = int(_vm_field(doc, doc_field) or 0)
	from_source = _bytes_to_gib_ceil(int(source_bytes or 0))
	return max(declared, from_source)


def _server_ipv4(server: str) -> str:
	return frappe.db.get_value("Server", server, "ipv4_address")


def _server_title(server: str) -> str:
	"""A human-readable host name for the progress line (the Server's title, e.g.
	`f1-aditya-blr3`), falling back to the row name if a title isn't set."""
	return frappe.db.get_value("Server", server, "title") or server


# Human-readable, present-tense line per phase, naming the host the work runs on —
# stamped BEFORE the phase runs so the form is never blank about what's happening.
# Long phases (Hydrating, and the base-image ship inside TargetPreparing) overwrite
# this with a finer-grained line + a percent as they progress.
def _phase_label(doc, phase: str) -> str:
	source, target = _server_title(doc.source_server), _server_title(doc.target_server)
	return {
		"Pending": f"Stopping the VM on {source} for a cold, snapshot-free move.",
		"ExportingSnapshot": f"Snapshotting the disk and starting the NBD export on {source}.",
		"TargetPreparing": f"Preparing the disk clone on {target}.",
		"InjectingIdentity": f"Reserving the VM's address on {target}.",
		"Hydrating": f"Copying disk blocks from {source} to {target}.",
		"CutoverStarting": f"Cutting over to {target} (collapse clone, relaunch the VM).",
		"Repointing": "Re-pointing routing to the migrated VM.",
		"Cleanup": f"Tearing down migration scaffolding on {source}.",
	}.get(phase, phase)


def _progress(doc, detail: str, *, percent: int = -1) -> None:
	"""Write the always-current progress line (and, for a measurable copy, its
	percent) straight to the row via db_set so it is visible immediately — every
	tick, mid-phase, even while a long host task is still running. `percent=-1`
	means "not a measurable copy" and the form hides the bar."""
	doc.db_set({"progress_detail": detail, "progress_percent": percent})


def nbd_port(virtual_machine: str) -> int:
	"""A stable per-VM TCP port so concurrent migrations on one source host never
	collide. Derived like the other UUID-keyed values (tap/mac/uid)."""
	import uuid as _uuid

	return 10000 + (int(_uuid.UUID(virtual_machine).hex[:4], 16) % 5000)
