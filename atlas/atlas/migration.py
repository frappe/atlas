"""VM migration orchestration — the resumable phase machine and its callback.

See spec/24-vm-migration.md. This is the CONTROLLER-side driver; the host work
runs in the `migration-*` Task scripts (scripts/). Wired in hooks.py:

    scheduler_events = {"cron": {"*/2 * * * *": ["atlas.atlas.migration.reconcile_migrations"]}}

The design point: a migration is a sequence of idempotent host phases, each
recorded as the Migration row's `status`. `start_migration` (enqueued by
VirtualMachine.migrate on insert) is the migration's OWN driver: it runs one step
— a phase, or a single Hydrating poll — then re-enqueues itself to run the next,
looping until the row is terminal. So a migration walks Pending → … → CutoverStarting
(guest boots on the clone, DOWNTIME ENDS) → Hydrating(poll→…→100%, guest SERVING) →
CollapseClone → … → Done entirely on its own, self-pacing the long copy on the inline
SSH poll's round-trip, with no wait for a cron tick between steps. Boot-then-hydrate
(spec/24 §0) moved Hydrating AFTER CutoverStarting, so the copy overlaps uptime and is
off the downtime clock.

The `reconcile_migrations` cron is the SAFETY NET, not the driver — a dropped RQ
job, a provider rate-limit, an SSH blip, or a worker crash never strands a
migration, because the cron re-enters the recorded phase (idempotent) and every
phase resumes from the DB, never from in-memory state. (Historically the cron WAS
the Hydrating driver; the self-drive loop replaced that so a bench with the
scheduler off still finishes a migration.)

**Stage 1 (this build): change-address only.** The VM keeps its UUID (and every
host-local value derived from it) but gets a NEW public IPv6 on the target, and
the proxy/Subdomain layer is re-pointed to it. The keep-address paths (Scaleway
range-move §2, DigitalOcean permanent-forward §2.9) are later stages.

**Transport (this build): plain TCP.** The source binds `qemu-nbd` to its public
IPv4 and the target's `nbd-client` dials it directly — no SSH tunnel yet (the
host-to-host credential is a deferred stage-3 prerequisite, spec/24 §2.1). This
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
	address_is_free_on_server,
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
# Boot-then-hydrate order (spec/24 §0): the target boots on the dm-clone read-through
# in CutoverStarting, hydration runs while the guest SERVES, and CollapseClone
# transparently swaps the clone for a linear map once every block is local. The guest's
# downtime is now stop → export → prepare → inject → (keep-address: target-receive →
# source-forward) → boot — everything after boot is off the downtime clock.
PHASE_ORDER = (
	"Pending",
	"ExportingSnapshot",
	"TargetPreparing",
	"InjectingIdentity",
	"CutoverStarting",
	"Hydrating",
	"CollapseClone",
	"Repointing",
	"Cleanup",
	"Done",
)

# A phase Task stuck Running/Pending past this multiple of its timeout is treated
# as lost and the phase is re-entered.
LOST_TASK_TIMEOUT_FACTOR = 2


def clone_device_path(virtual_machine: str) -> str:
	"""The dm-clone read-through device for a migrated VM's root disk (spec/24 §0).
	Boot-then-hydrate boots the guest on this device; CollapseClone reloads its table
	to a linear map onto the plain LV. Named identically on the host
	(migration-clone-target's CLONE_DEV), a pure function of the UUID."""
	return f"/dev/mapper/atlas-vm-{virtual_machine}-clone"


# How many consecutive no-progress hydration polls before we give up.
HYDRATION_STALL_TICKS = 30

# Fast-stop grace period for the migration cold-stop (spec/24 §0.5.2). A migration
# discards the guest's RAM, so a long graceful-shutdown drain is wasted downtime; a
# few seconds is plenty for a clean guest halt, and ExecStopPost still fires on the
# escalation to SIGKILL so networking teardown is never skipped.
MIGRATION_STOP_TIMEOUT_SECONDS = 3


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
	"""Background entrypoint and the migration's OWN driver: advance a migration one
	phase (or run one Hydrating poll), then re-enqueue itself to do the next step —
	until the row reaches a terminal phase (Done/Failed). This is the "run actions one
	after another, and keep polling the long copy, as soon as they can" driver:
	VirtualMachine.migrate enqueues the first call on insert, and every step chains the
	next, so a migration walks Pending → … → Hydrating(poll→poll→…→100%) → … → Done
	entirely on its own, with no wait for a cron tick between steps.

	Crucially it re-enqueues even while Hydrating HOLDS (the multi-minute copy that
	polls each tick): the poll is inline SSH (~1-2s per iteration), so the self-drive
	loop is naturally paced by that round-trip — no artificial delay needed — and the
	migration hydrates to 100% by itself instead of depending on the scheduler. The
	`reconcile_migrations` cron is now a pure SAFETY NET: it re-drives any non-terminal
	row whose self-drive job was dropped (a worker crash, an OOM kill). It stops only
	at a terminal phase. Re-entrant and idempotent like the cron: it reloads and
	advances whatever phase the row records.

	One `long`-queue worker slot is held for the migration's duration (the pool ships 3
	workers); this is the same tradeoff Site/VM auto_provision makes — the job that owns
	the work stays resident until it's done."""
	if not frappe.db.exists("Virtual Machine Migration", name):
		return
	_reconcile_one(name)
	# Keep driving until the row is terminal — advanced phases AND a holding Hydrating
	# poll both re-enqueue, so the migration finishes on its own. _reconcile_one already
	# committed the new status (or marked the row Failed); re-read it to decide.
	status = frappe.db.get_value("Virtual Machine Migration", name, "status")
	if status not in ("Done", "Failed"):
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
	# Region is same-by-construction: one region per Atlas instance (spec/24 §1),
	# and Subdomain has no region field. Nothing to compare.

	# IPv6 on the target. Two schemes, two different gates (both probed here, read-only
	# intent, so the operator learns at click time rather than three phases deep):
	#   - change-address: allocate_ipv6 raises if the range is full. Authoritative
	#     allocation is in InjectingIdentity.
	#   - keep-address: no address is allocated (the VM keeps its /128, the source
	#     forwards it), so range fullness is irrelevant — BUT the kept /128 must not
	#     already be live on a DIFFERENT VM on the target, or the two collide on one
	#     host (a single `<vmv6>/128 dev <veth>` route can point at only one; the
	#     other VM silently steals the traffic — observed in the field). Authoritative
	#     re-check is in InjectingIdentity.
	if _will_keep_address(vm.server, target_server):
		_assert_kept_address_free(vm, target_server)
	else:
		_assert_ipv6_capacity(target_server)

	if vm.public_ipv4 and not release_reserved_ip:
		frappe.throw(
			"This VM has an attached public IPv4 (Reserved IP) bound to the source host. "
			"Stage-1 migration cannot move it; pass release_reserved_ip=True to acknowledge "
			"inbound v4 will be released, then re-attach a target-server Reserved IP afterward."
		)


def _will_keep_address(source_server: str, target_server: str) -> bool:
	"""Whether a migration between these two servers keeps the VM's /128 (spec/24
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


def _assert_kept_address_free(vm, target_server: str) -> None:
	"""keep-address gate: the VM's /128 must not already be live on a different VM on
	the target. Excludes the migrating VM's own row (a resume may have denormalized it
	onto the target already). The target's own native VMs allocate from ::2 up, so a
	source-::2 VM kept onto a target that already has a ::2 VM is a guaranteed
	collision — this is the check that stops it before the disks move."""
	if not address_is_free_on_server(target_server, vm.ipv6_address, ignore_vm=vm.name):
		frappe.throw(
			f"Cannot keep address {vm.ipv6_address}: target server {target_server} already "
			f"hosts a live VM on that /128. Two VMs cannot share a /128 on one host. "
			f"Terminate the conflicting VM or migrate to a different target."
		)


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
		# Fast-stop: a cold migration discards RAM, so bound the graceful drain to a
		# few seconds (spec/24 §0.5.2) — ExecStopPost (netns/veth/proxy-NDP teardown)
		# still fires, only the shutdown-grace wait is trimmed off the downtime clock.
		# graceful=False: no point ctrl-alt-del-ing a guest whose RAM (and clean
		# unmount) we're about to throw away — that only adds the shutdown wait back.
		vm.flags.migrating = True
		vm.stop(
			memory_snapshot=False,
			stop_timeout_seconds=MIGRATION_STOP_TIMEOUT_SECONDS,
			graceful=False,
		)
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
	the script skips any step whose artifact already exists.

	FIRST, if the VM's base image is LOCAL (snapshot-promoted, un-syncable — spec/24
	§5.1), ship it from the source to the target over NBD. That ship is a multi-GB,
	multi-tick copy, so this phase re-enters (returns False) until the base is fully
	received. A syncable/already-present base is a one-tick no-op."""
	if not _ensure_base_on_target(doc):
		return False  # base still shipping; re-enter next tick (progress on the row)

	_run_clone_prepare(doc)
	if doc.keep_address:
		_bring_up_forward_tunnel(doc)
	return True


def _run_clone_prepare(doc) -> None:
	"""Run the target `prepare` step: create the thin LV(s), connect the nbd client to
	the source, and build the dm-clone. Idempotent and self-repairing — the script
	skips healthy artifacts and rebuilds a wedged one (a dm-clone whose source nbd
	client has died). Shared by TargetPreparing (first build) and Hydrating (rebuild
	on a dropped NBD link)."""
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
			# Per-VM nbd device block on the target: root = base+0, data = base+1
			# (base+2/+3 belong to the base-image ship). Keeps concurrent migrations
			# to one target off each other's nbd devices.
			"NBD_BASE_SLOT": str(nbd_base_slot(doc.virtual_machine)),
			"PHASE": "prepare",
		},
		timeout_seconds=600,
	)


def _rebuild_clone_stack(doc) -> None:
	"""Re-establish a dm-clone whose source nbd client died mid-hydration. The prepare
	step detects the wedged stack (dead client under a live clone), removes the clone
	to free the nbd device, re-dials the client, and rebuilds the clone — the only way
	to recover, since the clone otherwise pins the dead device open (spec/24)."""
	_run_clone_prepare(doc)


def _ensure_base_on_target(doc) -> bool:
	"""Ship the VM's base image to the target if it is LOCAL and not already there.
	Returns True once the base is ready on the target (present, or ship complete),
	False while the multi-GB copy is still hydrating (so TargetPreparing re-enters).

	Only local (snapshot-promoted) images need this: a synced image is already on
	the target (or fails pre-flight early), and clone-target's own pre-flight will
	confirm presence. So for a non-local image this is a cheap DB-only no-op.

	Mechanism (spec/24 §5.1), mirroring the VM-disk ship exactly:
	  1. Source exports the read-only base LV + a tar of the image dir over NBD
	     (migration-export-base) — on the disk export's port +2 / +3.
	  2. Target hydrates a local base LV via dm-clone + extracts the image dir
	     (migration-receive-base PHASE=prepare), then we poll hydration to 100%
	     (migration-poll-hydration on the base clone device), then collapse
	     (migration-receive-base PHASE=finalize).
	The per-tick percent lands on base_ship_percent / progress_percent so the copy
	is visible throughout."""
	image = _vm_field(doc, "image")
	if not _image_is_local(image):
		return True  # syncable/standard image — clone-target handles presence itself.
	if doc.base_ship_state == "Done":
		return True  # already shipped in a prior tick.

	base_port = doc.nbd_port + 2  # disk root=port, data=port+1, base=port+2, meta=port+3
	source_title, target_title = _server_title(doc.source_server), _server_title(doc.target_server)

	# 1. Source export (idempotent — returns the running pids). Record the base size
	#    so the target's dest LV matches, and mark the ship in flight.
	if doc.base_ship_state != "Shipping":
		_progress(doc, f"Shipping base image {image} from {source_title} — starting export.", percent=0)
		doc.db_set({"base_ship_state": "Shipping", "base_ship_percent": 0})
	export = parse_result(
		_run_phase_task(
			doc,
			server=doc.source_server,
			script="migration-export-base",
			variables={
				"IMAGE_NAME": image,
				"NBD_PORT": str(base_port),
				"BIND_ADDRESS": _server_ipv4(doc.source_server),
			},
			timeout_seconds=120,
		).stdout
	)
	base_disk_gb = _bytes_to_gib_ceil(int(export["base_size_bytes"]))

	# 2. Target prepare: create the dest LV, dm-clone read-through, extract image dir.
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-receive-base",
		variables={
			"IMAGE_NAME": image,
			"DISK_GB": str(base_disk_gb),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(base_port),
			# base = base_slot+2, image-dir tar = base_slot+3 (root/data are +0/+1).
			"NBD_BASE_SLOT": str(nbd_base_slot(doc.virtual_machine)),
			"PHASE": "prepare",
		},
		timeout_seconds=300,
	)

	# 3. Poll hydration of the base clone device (same script as the VM disk, keyed
	#    on the base clone name). Re-enter until 100%.
	result = parse_result(
		_run_phase_task(
			doc,
			server=doc.target_server,
			script="migration-poll-hydration",
			variables={"CLONE_DEVICE": f"atlas-base-{image}-clone"},
			timeout_seconds=60,
		).stdout
	)

	# Self-heal a dead source, exactly as _phase_hydrating does for the VM disk: if the
	# nbd client backing the base clone has died, the copy is frozen (reads return 0
	# bytes) and the clone pins the dead device open. Re-running prepare (step 2 above)
	# now detects the wedged stack, removes the clone, re-dials the client, and rebuilds
	# — so we just re-enter and let the next tick's prepare do it. WITHOUT this, a
	# dropped NBD link mid-ship wedges forever (dm-clone spins on dead reads, log spam).
	if not result.get("source_healthy", True):
		_progress(
			doc,
			f"NBD link to {source_title} dropped mid base-image ship — rebuilding the "
			f"base clone on {target_title} and resuming.",
			percent=doc.base_ship_percent or 0,
		)
		doc.db_set("base_ship_percent", 0)
		return False  # re-enter; next tick's prepare rebuilds the wedged stack

	percent = int(result["hydration_percent"])
	doc.db_set("base_ship_percent", percent)
	_progress(doc, f"Shipping base image {image} to {target_title} — {percent}% copied.", percent=percent)
	if percent < 100:
		return False  # still copying — TargetPreparing re-enters next tick.

	# 4. Collapse the base clone to a plain read-only local base image.
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-receive-base",
		variables={
			"IMAGE_NAME": image,
			"DISK_GB": str(base_disk_gb),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(base_port),
			"NBD_BASE_SLOT": str(nbd_base_slot(doc.virtual_machine)),
			"PHASE": "finalize",
		},
		timeout_seconds=120,
	)
	doc.db_set({"base_ship_state": "Done", "base_ship_percent": 100})
	_progress(doc, f"Base image {image} shipped to {target_title}; preparing the disk clone.", percent=-1)
	return True


def _bring_up_forward_tunnel(doc) -> None:
	"""keep-address only: create the per-VM forward tunnel on BOTH hosts (spec/24
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
	"""Decide the address the VM will boot with on the target, record it, and inject
	the VM's identity THROUGH the dm-clone device — before the guest boots on it in
	CutoverStarting (spec/24 §0). Two steps, each with its own resume key:

	1. Address (resume key: ipv6_address_new set):
	   - change-address: allocate a NEW /128 from the target's range. allocate_ipv6
	     holds the target Server row for_update — atomic, so two parallel migrations
	     can't grab the same address. Persist before advancing so a crash re-uses the
	     same address on re-entry (throws if the range filled since pre-flight).
	   - keep-address: NEAR-NO-OP for networking (spec/24 §2.9.4). The /128 is
	     unchanged — the source keeps holding the /64 and forwards it — so there is NO
	     allocate; the VM boots on the SAME address. Record the unchanged address as
	     ipv6_address_new so the shared boot path launches it on the right /128.

	2. Identity inject THROUGH the clone (resume key: identity_injected flag). The
	   plain atlas-vm-<uuid> LV mounts BUSY under the live clone (host-verified
	   2026-07-02, spec/24 §0.4), so we mount the CLONE device; writes land on the
	   dest and count toward hydration. Host keys are PRESERVED (the disk moved
	   wholesale; its SSH identity must survive the move). This moved earlier from
	   cutover so the guest can boot the instant target-receive/source-forward arm —
	   provision-vm at boot then does NOT re-inject (boot-on-clone)."""
	if not doc.ipv6_address_new:
		if doc.keep_address:
			# Authoritative keep-address collision re-check (pre-flight probed it at
			# click time; re-assert here in case a VM claimed the /128 on the target
			# in between). No allocate — the address is carried unchanged.
			if not address_is_free_on_server(
				doc.target_server, doc.ipv6_address_old, ignore_vm=doc.virtual_machine
			):
				frappe.throw(
					f"Cannot keep address {doc.ipv6_address_old}: target server "
					f"{doc.target_server} already hosts a live VM on that /128."
				)
			address = doc.ipv6_address_old
		else:
			address = allocate_ipv6(doc.target_server)
		doc.db_set("ipv6_address_new", address)

	if not doc.identity_injected:
		# Pull the identity fields off the SAME _provision_variables() the cutover
		# boot uses, then re-target the address to ipv6_address_new (the doc still
		# points at the source /128 until Repointing) — so a change-address inject
		# writes the NEW /128's network.env, and keep-address writes the unchanged one.
		vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
		provision = vm._provision_variables()
		host_cidr, guest_cidr = derive_ipv4_link(doc.ipv6_address_new)
		_run_phase_task(
			doc,
			server=doc.target_server,
			script="migration-inject-identity",
			variables={
				"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
				"CLONE_DEVICE": clone_device_path(doc.virtual_machine),
				"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_new,
				"IPV4_GUEST_CIDR": guest_cidr,
				"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
				"SSH_PUBLIC_KEY": provision["SSH_PUBLIC_KEY"],
				"DATA_DISK_MOUNT_AT": provision.get("DATA_DISK_MOUNT_AT", ""),
				"ROUTING_BASE_URL": provision.get("ROUTING_BASE_URL", ""),
			},
			timeout_seconds=120,
		)
		doc.db_set("identity_injected", 1)
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

	# Self-heal a dead source: if the nbd client backing the clone has died (reads
	# return 0 bytes, hydration frozen), the copy can't progress in place — the clone
	# pins the nbd device open, so it must be torn down and rebuilt. Re-run the
	# TargetPreparing prepare step, which now detects the wedged stack, removes the
	# clone, re-dials the client, and recreates the clone; hydration resumes (from 0)
	# on the next tick. We do NOT count this toward the stall guard — it is a
	# recoverable transport failure, not a genuinely stuck copy.
	if not result.get("source_healthy", True):
		_progress(
			doc,
			f"NBD link to {_server_title(doc.source_server)} dropped — rebuilding the "
			f"disk clone on {_server_title(doc.target_server)} and resuming hydration.",
			percent=doc.hydration_percent or 0,
		)
		_rebuild_clone_stack(doc)
		# The rebuilt clone hydrates from 0; reset the tracked percent + stall counter
		# so the next poll measures the fresh copy, not the stale 58%.
		doc.db_set({"hydration_percent": 0, "hydration_stall_ticks": 0})
		return False  # re-enter next tick; the rebuilt clone hydrates afresh

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
	"""Boot-then-hydrate cutover (spec/24 §0) — this is where DOWNTIME ENDS. The
	guest boots on the dm-clone read-through and starts serving; hydration then runs
	off the clock (the next phase) while the guest is up. NO collapse here — the clone
	is collapsed only after hydration reaches 100% (CollapseClone).

	Order matters for keep-address (spec/24 §2.3): the forward path must be armed
	BEFORE the guest can serve, or the guest is up but black-holed. So:

	0. Private plane (spec/31 §16.3): withdraw the VM's private /128 from the SOURCE
	   host's ownership cache BEFORE provision-vm boots the target and starts
	   advertising the SAME /128. The /128 is host-independent (survives the move
	   byte-for-byte), so two hosts advertising it concurrently is the §7.3 conflict
	   — ANCP drops it from every host's wg-mesh AllowedIPs and the migrated VM's
	   private plane blackholes for the whole hydration window. Withdrawing first
	   keeps the two advertisements non-overlapping (§16.3 withdraw-from-source THEN
	   advertise-on-target). Safe here: the source VM is Stopped from Pending until
	   Cleanup (spec/24 §0.3), so the source stopped SERVING the /128 long ago — this
	   only stops it ADVERTISING; it does not blackhole a live source VM.
	1. keep-address only: `migration-target-receive` (return route) then
	   `migration-source-forward` (point the /128 at the tunnel + re-assert proxy-NDP).
	   Armed before boot so the first inbound packet is deliverable the instant the
	   guest is up.
	2. `provision-vm` with CLONE_ROOTFS_DEVICE set: boots the guest on
	   /dev/mapper/atlas-vm-<uuid>-clone. It does NOT re-inject (done in
	   InjectingIdentity through the clone), does NOT re-create the disk (the clone's
	   dest LV exists and is hydrating), and exposes the CLONE device as the jail
	   rootfs node. `--no-block` returns once the start is queued. Its ExecStartPre
	   (vm-network-up.py) ADD_LOCAL_OWNs the /128 on the target — the advertise that
	   step 0's withdraw must precede.
	3. Mark the VM Running + flip `server` to the target: the row now reflects
	   "live on target", stopping the downtime clock. (change-address Subdomain
	   re-point + reserved-IP handling stay in Repointing, off the clock.)

	Resume key: every step is idempotent — the source-withdraw is a no-op once the
	/128 is already out of the cache, the forward scripts re-assert, provision-vm
	re-exposes the same node + re-launches, and _finalize_cutover no-ops once the row
	already points at the target."""
	_withdraw_private_from_source(doc)

	if doc.keep_address:
		_install_forward_routes(doc)

	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	variables = vm._provision_variables()
	host_cidr, guest_cidr = derive_ipv4_link(doc.ipv6_address_new)
	variables.update(
		{
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_new,
			"IPV4_HOST_CIDR": host_cidr,
			"IPV4_GUEST_CIDR": guest_cidr,
			"IPV4_GATEWAY": str(ipaddress.ip_interface(host_cidr).ip),
			# preserve_host_keys is moot on the boot-on-clone path (provision-vm skips
			# inject entirely — identity was injected through the clone earlier), but
			# pass it for symmetry / the safety of any future inject-at-boot path.
			"PRESERVE_HOST_KEYS": "1",
			# THE boot-then-hydrate switch: boot on the clone read-through device.
			"CLONE_ROOTFS_DEVICE": clone_device_path(doc.virtual_machine),
		}
	)
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="provision-vm",
		variables=variables,
		timeout_seconds=120,
	)
	# The guest is now live on the target. Commit the row so status/server reflect it
	# and the downtime window closes here, not after the multi-minute copy.
	_finalize_cutover(doc)
	return True


def _phase_collapse_clone(doc) -> bool:
	"""Collapse the now-100%-hydrated dm-clone(s) TRANSPARENTLY while the guest is live
	(spec/24 §0.4). `migration-cutover-target` suspends the clone, reloads its table
	from `clone` to a `linear` map onto the plain dest LV, and resumes — the dm device
	keeps the SAME major:minor, so Firecracker's open rootfs fd survives (host-verified
	on real f1 thin LVs, 2026-07-02: `dmsetup remove` on an open fd fails "Device or
	resource busy"; reload-to-linear does not). The source nbd client is disconnected.

	Resume key: the script no-ops on a clone that already carries a linear table (a
	re-entry after collapse) or is already gone."""
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-cutover-target",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"DATA_DISK_GB": str(_vm_field(doc, "data_disk_gigabytes") or 0),
			# Same per-VM nbd block clone-target used, so collapse disconnects the RIGHT
			# devices (root = base+0, data = base+1) — never another migration's.
			"NBD_BASE_SLOT": str(nbd_base_slot(doc.virtual_machine)),
		},
		timeout_seconds=120,
	)
	return True


def _install_forward_routes(doc) -> None:
	"""keep-address only: now that the target VM is up on the SAME /128, wire the
	traffic path (spec/24 §2.2-2.3, §2.9.2-2.9.3). Target return-route FIRST (so the
	guest's replies have somewhere to go the instant inbound starts arriving), then
	the source forward (which points the /128 delivery at the tunnel and — on a
	proxy-NDP provider — re-asserts the NDP entry the source unit's stop removed).
	Idempotent: both scripts re-assert with `replace`/duplicate-guarded adds. Moves
	tunnel_status to Forwarding — the path is now live and stays up permanently."""
	tunnel_device = doc.tunnel_device or derive_vm_tunnel(doc.virtual_machine)
	route_table = derive_vm_tunnel_table(doc.virtual_machine)
	# Re-lay each socat unit with the route now known, so it is baked into the unit's
	# ExecStartPost: a carrier restart recreates a bare tun device, and only an
	# ExecStartPost re-applies addr/MTU/route onto it (the Python route scripts below
	# assert the route ONCE, on the current device, and would not survive a restart).
	# The route scripts still run — they own the nft rules / (DO) proxy-NDP that
	# forward-up does not — but the durable route now lives in the unit.
	_relay_forward_tunnel(doc, tunnel_device, route_table)
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
			# ALWAYS re-assert proxy-NDP, every provider (not just DigitalOcean). The
			# upstream switch delivers a /128 to the host only because the host answers
			# NDP for it — vm-network-up.py does this unconditionally at provision
			# (line ~197), for Scaleway too. vm-network-down removed it at cutover, so
			# WITHOUT this re-assert the source stops answering NDP and the switch
			# black-holes ALL inbound to the /128 (proven in the field: egress works,
			# public ingress 0% — the earlier "Scaleway routed /64 needs no NDP"
			# assumption was wrong for these Elastic Metal hosts).
			"REASSERT_PROXY_NDP": "1",
		},
		timeout_seconds=60,
	)
	doc.db_set({"tunnel_status": "Forwarding", "forward_active": 1})


def _relay_forward_tunnel(doc, tunnel_device: str, route_table: int) -> None:
	"""keep-address only: re-run migration-forward-up on BOTH hosts WITH the route
	args, so each socat unit's ExecStartPost now re-lays this side's traffic route on
	every (re)start. At TargetPreparing the tunnel came up bare (no routes); this is
	the cutover upgrade that makes the whole path — not just the carrier — survive a
	socat restart. Idempotent: forward-up stops+re-lays the unit (a running carrier
	blips for RestartSec, immediately reconnects)."""
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
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_old,
			"ROUTE_TABLE": str(route_table),
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
			"VIRTUAL_MACHINE_IPV6": doc.ipv6_address_old,
			"ROUTE_TABLE": str(route_table),
		},
		timeout_seconds=60,
	)


def _phase_repointing(doc) -> bool:
	"""The point of no return — all Frappe-side. Commit the VM row to the target
	(and, change-address only, the new address), then re-point every Subdomain.
	Idempotent: a second run sets the same values and reconciles the same
	(already-converged) map.

	keep-address (spec/24 §2.9.4): the /128 never changed, so the Subdomain rows are
	already correct and the proxy already dials the right address — the SUBDOMAIN
	RE-POINT AND RECONCILE ARE SKIPPED ENTIRELY. `server` still flips (the VM really
	is on the target now); the address is copied verbatim."""
	_finalize_cutover(doc)
	if not doc.keep_address:
		_repoint_routes(doc)
	_handle_reserved_ip(doc)
	_repoint_private_plane(doc)
	return True


def _withdraw_private_from_source(doc) -> None:
	"""Stop the SOURCE host advertising the VM's private /128, BEFORE the target's
	provision-vm boots the guest and starts advertising the SAME /128 (spec/31 §16.3).
	This is the withdraw half of the §16.3 withdraw-from-source-THEN-advertise-on-target
	ordering the migration controller owns — the ordering the retired
	`sequenced_migration_cutover` used to give via a hard fleet-wide push. Because the
	/128 is HOST-INDEPENDENT (a pure HKDF of tenant+VM — it is the same string on both
	hosts), two hosts advertising it at once is the §7.3 conflict: ANCP drops it from
	every host's wg-mesh AllowedIPs, blackholing the migrated VM's private plane for the
	whole hydration window. Withdrawing first keeps the two advertisements non-overlapping.

	Runs `migration-withdraw-private-source` on the SOURCE — it removes ONLY the /128 from
	the source's local-ownership cache (no netns/veth/disk/LV work), so it never disturbs
	the intact source copy the rollback-through-Hydrating path (spec/24 §0.3) depends on.
	Safe: the source VM is Stopped from Pending until Cleanup (spec/24 §0.3), so it stopped
	SERVING the /128 at Pending — this only stops it ADVERTISING; a live source VM is never
	blackholed. Normally a re-assert (the source unit's Pending stop already withdrew it via
	vm-network-down.py's remove_local_owned), kept EXPLICIT so the ordering is controller-
	guaranteed at the cutover seam rather than an incidental side effect of the stop.
	No-op for a tenant-less VM (no private /128): the task's private_address arrives empty
	and remove_local_owned is skipped. Idempotent — remove_local_owned no-ops on an
	absent /128, so a re-entered CutoverStarting re-runs cleanly."""
	vm_tenant = frappe.db.get_value("Virtual Machine", doc.virtual_machine, "tenant")
	if not vm_tenant:
		return
	_run_phase_task(
		doc,
		server=doc.source_server,
		script="migration-withdraw-private-source",
		variables={
			"VIRTUAL_MACHINE_NAME": doc.virtual_machine,
			"PRIVATE_ADDRESS": _vm_field(doc, "private_address") or "",
		},
		timeout_seconds=60,
	)


def _repoint_private_plane(doc) -> None:
	"""The private /128 moves with the VM to the target host (spec/31 §16.3 — soft
	sequencing). The address is HOST-INDEPENDENT (survives the move byte-for-byte); only
	which host advertises it changes. This runs in Repointing AFTER the cutover has already
	swapped the advertiser: the withdraw-from-source is done in CutoverStarting
	(`_withdraw_private_from_source`, spec/31 §16.3), and the target began advertising the
	/128 when its provision-vm booted the guest there in the same phase (vm-network-up.py's
	`add_local_owned`) — so by the time this runs the source has stopped and the target has
	started advertising, non-overlapping. Nothing is left for this function to push: ANCP
	gossip has already propagated both updates and the §16.3 non-overlap invariant holds at
	each host (atomic whole-table `wg syncconf` + conflict-driven drop). It stays as an
	explicit, documented no-op (not deleted) so the private-plane cutover has a named seam
	in the Repointing phase and the retirement of the old controller-side
	`sequenced_migration_cutover` (spec/31 §6 — no more fleet-wide SSH pushes; §17.2 bounds
	the eventual-consistency window to ~4.6 s at the default timers) is visible here. No-op
	for a tenant-less VM (no private /128)."""
	vm_tenant = frappe.db.get_value("Virtual Machine", doc.virtual_machine, "tenant")
	if not vm_tenant:
		return


def _phase_cleanup(doc) -> bool:
	"""Source: kill NBD, lvremove the -migrate snapshots, tear down the stale source
	copy (old dir/LVs/netns). If it fails, the row stays at Cleanup with the error —
	there is no orphaned-LV reconciler, so the row IS the backstop.

	keep-address (spec/24 §2.9.4): the SAME source teardown runs (the stale disk copy
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
	the source host (spec/24 §2.9.5). Drives the VM-form dashboard indicator and
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
	"CutoverStarting": _phase_cutover_starting,
	"Hydrating": _phase_hydrating,
	"CollapseClone": _phase_collapse_clone,
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
	(spec/24 §2.9.5). The forward is permanent by default; this is the ONLY point at
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
	# 1b. Source end: remove the /128 route, nft rules, and the proxy-NDP entry.
	#     Deassert proxy-NDP for EVERY provider (mirror of the unconditional re-assert
	#     in _install_forward_routes) — the source answered NDP for the /128 while
	#     forwarding, so collapse must stop it on all providers, not just DigitalOcean.
	run_task(
		server=source_server,
		script="migration-forward-down",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"VIRTUAL_MACHINE_IPV6": old_ipv6,
			"ROLE": "source",
			"TUNNEL_DEVICE": tunnel_device,
			"TUNNEL_PORT": str(tunnel_port),
			"DEASSERT_PROXY_NDP": "1",
		},
		virtual_machine=vm.name,
		timeout_seconds=60,
	)

	# 2. Allocate a fresh /128 on the current host and re-provision the VM onto it.
	#    Skip the allocate if a prior collapse attempt already moved the VM off
	#    old_ipv6.
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
	# STOP the VM first, for two reasons: (a) collapse runs on a LIVE VM, and
	# provision-vm's `systemctl start` is a no-op on an already-running unit — the
	# guest would never reboot onto the new /128 (its host veth route + guest eth0 are
	# re-laid only at unit (re)start). (b) A boot-then-hydrate migration left the disk
	# behind a collapsed-linear dm-clone that holds the plain LV BUSY; stop-vm CONVERGES
	# that clone (removes it once the guest's fd is released), so the plain LV is then
	# directly mountable and provision-vm's ordinary inject+launch just works. This is a
	# brief operator-initiated blip, not a latency-critical cutover.
	vm.reload()
	if vm.status == "Running":
		vm.flags.migrating = True
		vm.stop(memory_snapshot=False)
	run_task(
		server=vm.server,
		script="provision-vm",
		variables=variables,
		virtual_machine=vm.name,
		timeout_seconds=120,
	)

	# 3. Commit the new address on the VM row, clear the forward markers, then
	#    re-point the Subdomains at it (the change-address path — now the address
	#    really did change). db_set (not save): it bypasses the optimistic-lock
	#    timestamp check — the long-running host tasks above leave a stale in-memory
	#    doc, and a trailing migration self-drive tick may have touched the row in the
	#    meantime (a save() would raise TimestampMismatchError) — and it skips the
	#    validate() immutability gate on ipv6_address cleanly (these are the sanctioned
	#    post-cutover writes, like _finalize_cutover's).
	vm.db_set(
		{
			"ipv6_address": new_ipv6,
			"status": "Running",
			"traffic_forwarded_from": None,
			"traffic_forwarded_since": None,
		}
	)
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


def _image_is_local(image_name: str) -> bool:
	"""True if the VM's base image was promoted from a snapshot (`is_local`) and so
	has no rootfs URL to sync — it lives only on the host it was promoted on, and a
	migration must SHIP it to the target (spec/24 §5.1) rather than assume sync.

	`is_local` is a computed property on Virtual Machine Image (no rootfs URL), not a
	stored column, so we replicate its one-line definition off the DB field to avoid
	loading the whole doc every tick."""
	rootfs_url = frappe.db.get_value("Virtual Machine Image", image_name, "rootfs_url")
	return not (rootfs_url or "").strip()


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
		"InjectingIdentity": f"Reserving the VM's address and injecting identity on {target}.",
		"CutoverStarting": f"Cutting over: booting the VM on {target} (reading through the clone).",
		"Hydrating": f"Copying disk blocks from {source} to {target} (VM serving).",
		"CollapseClone": f"Collapsing the disk clone to local storage on {target}.",
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


# Each migration's TARGET side needs a contiguous block of nbd CLIENT devices:
# root disk, data disk, base-image ship, base-image-dir tar — 4 slots. Hosts ship
# 16 nbd devices (nbds_max=16), so a per-VM base slot of (uuid % 4) * 4 fans four
# concurrent migrations across /dev/nbd0-15 with no overlap. WITHOUT this the disk
# clone hardcoded /dev/nbd0 & /dev/nbd1, so a second migration to the same target
# latched onto the first's live nbd0 (wrong size → dm-clone "Invalid argument") —
# found on a real double-migration to f2 (2026-07-02). Derived (not allocated) so
# the controller and every host script agree from the UUID with no stored state.
NBD_SLOTS_PER_MIGRATION = 4
MAX_CONCURRENT_TARGET_MIGRATIONS = 4  # 4 * 4 = 16 = nbds_max


def nbd_base_slot(virtual_machine: str) -> int:
	"""The first of this VM's 4 contiguous nbd client slots on the TARGET host:
	base+0 root, base+1 data, base+2 base-image, base+3 image-dir tar. A pure
	function of the UUID (like nbd_port), so clone/cutover/base-ship all name the
	same devices with no allocator."""
	import uuid as _uuid

	index = int(_uuid.UUID(virtual_machine).hex[4:8], 16) % MAX_CONCURRENT_TARGET_MIGRATIONS
	return index * NBD_SLOTS_PER_MIGRATION
