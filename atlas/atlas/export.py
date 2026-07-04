"""Base-image export orchestration — the resumable phase machine and its callback.

See spec/08-images.md § "Two origins for a base image" and spec/24-vm-migration.md
§5.1. This ships a LOCAL (snapshot-promoted, un-syncable) base image from the host
it was promoted on to another server, so a VM on the target can provision from it
via the ordinary `image` field. It is the standalone form of the base-image ship a
VM migration runs as a sub-step (migration._ensure_base_on_target) — same host
scripts, same dm-clone hydrate-then-collapse, but with no VM, no addressing, and no
cutover.

Wired in hooks.py:

    scheduler_events = {"cron": {"*/2 * * * *": ["atlas.atlas.export.reconcile_image_exports"]}}

The design point mirrors migration.py exactly: an export is a sequence of idempotent
host phases, each recorded as the Export row's `status`. `start_export` (enqueued by
export_image on insert) is the export's OWN driver — it runs one step (a phase, or a
single Hydrating poll) then re-enqueues itself for the next, looping until the row is
terminal, self-pacing the multi-GB copy on the inline poll's round-trip with no wait
for a cron tick. `reconcile_image_exports` is the SAFETY NET: a dropped RQ job never
strands an export, because the cron re-enters the recorded phase (idempotent) and
every phase resumes from the DB.

**Transport: plain TCP, unencrypted, no SSH.** The source binds `qemu-nbd` to its
public IPv4 and the target's `nbd-client` dials it directly (spec/24 §2.1). The base
LV is read-only and immutable, so this is a one-shot cold copy — none of the live-VM
cutover machinery, no keep-address, no memory pair.

Every phase obeys two rules, like migration's:
  1. It runs its host work INLINE via run_task (not frappe.enqueue), which saves the
     Task row first and raises on failure — never a lost worker job.
  2. It is idempotent: it checks its resume key before acting, so re-entering a
     half-finished phase is safe.
"""

from __future__ import annotations

import uuid as _uuid

import frappe

from atlas.atlas.ssh import run_task
from atlas.atlas.task_results import parse_result

# Phase order. The scheduler advances a row from one to the next; each name is also a
# key in PHASES below. Done/Failed are terminal (handled by the row).
#
#   Exporting   — source: activate the base LV + image-dir tar, start the NBD exports.
#   Hydrating   — target: prepare the dm-clone read-through, then poll to 100%.
#   Finalizing  — target: collapse the clone to a plain read-only local base LV.
#   Registering — controller: insert the local Virtual Machine Image row on the target.
#   Cleanup     — source: stop the NBD exports and drop the staged tar.
PHASE_ORDER = (
	"Pending",
	"Exporting",
	"Hydrating",
	"Finalizing",
	"Registering",
	"Cleanup",
	"Done",
)

# A phase Task stuck Running/Pending past this multiple of its timeout is treated as
# lost and the phase is re-entered.
LOST_TASK_TIMEOUT_FACTOR = 2

# How many consecutive no-progress hydration polls before we give up.
HYDRATION_STALL_TICKS = 30


def base_clone_device(image: str) -> str:
	"""The dm-clone read-through device the target builds while hydrating a shipped
	base image. Named identically on the host (migration-receive-base's CLONE_DEV),
	a pure function of the image name — the poll phase names it to read progress."""
	return f"atlas-base-{image}-clone"


# ─────────────────────────────────────────────────────────────────────────────
# Entry: synchronous kick from the Image form action, and the two drivers.
# ─────────────────────────────────────────────────────────────────────────────


@frappe.whitelist()
def export_image(image: str, target_server: str, source_server: str | None = None) -> str:
	"""Create + kick a base-image export. Called from the Virtual Machine Image form's
	`Export Image` action (the operator picks a target server). Returns the export
	row's name.

	Only a LOCAL image needs this: a from-URL image is placed on any server by
	`sync-image` (Sync to All Servers), so exporting it would be redundant. We reject a
	syncable image up front rather than run a copy the operator should do via sync.

	`source_server` is optional: a local image lives on exactly one host, resolved from
	the promote Task history (before_insert). Pass it explicitly to override that
	resolution (e.g. an image re-promoted on a second host)."""
	from atlas.atlas.doctype.virtual_machine_image_export.virtual_machine_image_export import (
		active_export_for,
	)

	preflight_checks(image, target_server, source_server)
	if active_export_for(image, target_server):
		frappe.throw(f"{image} already has an in-flight export to {target_server}")

	doc = frappe.get_doc(
		{
			"doctype": "Virtual Machine Image Export",
			"image": image,
			"target_server": target_server,
			"source_server": source_server or None,
		}
	).insert()
	# nosemgrep: frappe-manual-commit -- persist the Pending export row before enqueuing
	# start_export so the background job can find it cross-transaction
	frappe.db.commit()

	frappe.enqueue(
		"atlas.atlas.export.start_export",
		queue="long",
		timeout=1800,
		name=doc.name,
	)
	return doc.name


def preflight_checks(image: str, target_server: str, source_server: str | None) -> None:
	"""The cheap, synchronous gate — DB-answerable rejections before a row is made.
	On-host checks (pool headroom, kernel modules) run inside the phases where SSH is
	in hand (migration-receive-base's own pre-flight)."""
	from atlas.atlas.doctype.virtual_machine_image_export.virtual_machine_image_export import (
		_image_home_server,
	)

	row = frappe.db.get_value("Virtual Machine Image", image, ["is_active", "rootfs_url"], as_dict=True)
	if not row:
		frappe.throw(f"Virtual Machine Image {image} does not exist")
	# A from-URL image is placed by sync-image; only a local (URL-less) image needs a
	# host-to-host copy. `is_local` is "no rootfs URL" (VirtualMachineImage.is_local).
	if (row.rootfs_url or "").strip():
		frappe.throw(
			f"{image} is a from-URL image — place it on a server with Sync to All Servers, "
			"not Export (which ships a local, un-syncable image's bytes)."
		)

	resolved_source = source_server or _image_home_server(image)
	if not resolved_source:
		frappe.throw(
			f"Cannot resolve which server holds {image}'s base LV (no successful promote "
			"Task found). Pass source_server explicitly."
		)
	if resolved_source == target_server:
		frappe.throw(f"{image} already lives on {target_server}")

	target = frappe.db.get_value("Server", target_server, ["status", "provider_type"], as_dict=True)
	if not target:
		frappe.throw(f"Target server {target_server} does not exist")
	if target.status != "Active":
		frappe.throw(f"Target server {target_server} is not Active (status is {target.status})")

	# Same provider: the transport is plain-TCP NBD over the source's public IPv4;
	# cross-provider is untested and out of scope, matching migration pre-flight.
	source_provider = frappe.db.get_value("Server", resolved_source, "provider_type")
	if source_provider != target.provider_type:
		frappe.throw(
			"Cross-provider export is out of scope (source and target must share a provider): "
			f"{source_provider} != {target.provider_type}"
		)


def reconcile_image_exports() -> None:
	"""Scheduler entry (the 'callback'). Advance every non-terminal export one step.
	Try/except PER ROW: one wedged export never blocks the others. Re-entrant by
	construction — if the previous tick crashed mid-phase, this tick re-enters the
	same phase (idempotent), so nothing is lost and nothing double-runs."""
	names = frappe.get_all(
		"Virtual Machine Image Export",
		filters={"status": ["not in", ("Done", "Failed")]},
		pluck="name",
	)
	for name in names:
		_reconcile_one(name)


def start_export(name: str) -> None:
	"""Background entrypoint and the export's OWN driver: advance one phase (or run one
	Hydrating poll), then re-enqueue itself for the next step — until the row is
	terminal. export_image enqueues the first call on insert, and every step chains the
	next, so an export walks Pending → … → Hydrating(poll→…→100%) → … → Done on its own,
	self-paced by the inline poll's round-trip with no wait for a cron tick. The
	`reconcile_image_exports` cron is a pure SAFETY NET for a dropped self-drive job."""
	if not frappe.db.exists("Virtual Machine Image Export", name):
		return
	_reconcile_one(name)
	status = frappe.db.get_value("Virtual Machine Image Export", name, "status")
	if status not in ("Done", "Failed"):
		frappe.enqueue(
			"atlas.atlas.export.start_export",
			queue="long",
			timeout=1800,
			name=name,
		)


def _reconcile_one(name: str) -> bool:
	"""Advance one export a single phase, committing progress on success and marking it
	Failed on error — in isolation, so one wedged row never blocks or rolls back
	another. Shared by the cron and the on-insert kick. Returns True iff the row
	advanced to a further non-terminal phase (more work to run immediately)."""
	try:
		advanced = advance_export(frappe.get_doc("Virtual Machine Image Export", name))
		# nosemgrep: frappe-manual-commit -- persist each export's progress independently
		# so one row's later failure can't roll back another's
		frappe.db.commit()
		return advanced
	except Exception as exception:
		frappe.db.rollback()
		_fail(name, str(exception))
		frappe.logger("atlas").error(f"image export {name} failed: {exception}")
		return False


def advance_export(doc) -> bool:
	"""Run the phase recorded on the row, then advance the status on success. Returns
	True iff the row advanced to a further NON-terminal phase (the caller should drive
	the next phase now). Returns False when the phase held (Hydrating polling) or
	reached a terminal phase (Done).

	Resumability: we ALWAYS re-derive what to do from `doc.status`, never a carried
	cursor. Each phase first checks its resume key, so a re-entry after a crash is a
	cheap no-op up to where it got."""
	phase = doc.status
	if phase not in PHASE_ORDER or phase == "Done":
		return False
	_progress(doc, _phase_label(doc, phase), percent=-1)
	handler = PHASES[phase]
	completed = handler(doc)
	if not completed:
		return False
	nxt = PHASE_ORDER[PHASE_ORDER.index(phase) + 1]
	updates = {"status": nxt, "progress_percent": -1}
	if nxt == "Done":
		updates["completed_at"] = frappe.utils.now_datetime()
		updates["progress_detail"] = "Export complete."
	doc.db_set(updates)
	return nxt != "Done"


# ─────────────────────────────────────────────────────────────────────────────
# Phases. Each returns True (advance) or False (re-enter next tick).
# ─────────────────────────────────────────────────────────────────────────────


def _phase_pending(doc) -> bool:
	"""Nothing on the host — the base LV is read-only and static, so there is no VM to
	stop and no snapshot to take. Just advance into Exporting. (Kept as an explicit
	phase so the row's status walk reads the same shape as a migration's.)"""
	return True


def _phase_exporting(doc) -> bool:
	"""Source: activate the read-only base LV and start the NBD exports — the LV on
	`export_port`, a tar of the image directory (kernel + rootfs sentinel) on
	`export_port + 1`. Idempotent: the script re-uses already-serving qemu-nbd
	processes; we just re-record the port/pid and base size."""
	result = parse_result(
		_run_phase_task(
			doc,
			server=doc.source_server,
			script="migration-export-base",
			variables={
				"IMAGE_NAME": doc.image,
				"NBD_PORT": str(export_port(doc.image)),
				"BIND_ADDRESS": _server_ipv4(doc.source_server),
			},
			timeout_seconds=120,
		).stdout
	)
	doc.db_set(
		{
			"nbd_port": int(result["nbd_port"]),
			"nbd_pid": int(result["nbd_pid"]),
			"base_size_bytes": int(result["base_size_bytes"]),
		}
	)
	return True


def _phase_hydrating(doc) -> bool:
	"""Target: build the dm-clone read-through from the source's NBD export (prepare),
	then poll hydration to 100%. The ONLY non-advancing phase: returns False until
	100% so the scheduler re-enters it each tick — a multi-minute copy becomes a series
	of cheap, read-only probes that never hold a worker. Stall guard: no progress for
	HYDRATION_STALL_TICKS → raise (→ Failed).

	`prepare` is idempotent and self-repairing (it skips healthy artifacts and rebuilds
	a wedged clone whose nbd client died), so re-running it every tick is safe and heals
	a dropped link."""
	base_disk_gb = _bytes_to_gib_ceil(int(doc.base_size_bytes or 0))
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-receive-base",
		variables={
			"IMAGE_NAME": doc.image,
			"DISK_GB": str(base_disk_gb),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(doc.nbd_port),
			# Standalone export: no VM contends for nbd slots on the target, so the
			# default per-VM block (base slot 0 → base image on nbd2, image-dir tar on
			# nbd3) is free. migration-receive-base uses these offsets internally.
			"PHASE": "prepare",
		},
		timeout_seconds=300,
	)

	percent = int(
		parse_result(
			_run_phase_task(
				doc,
				server=doc.target_server,
				script="migration-poll-hydration",
				variables={"CLONE_DEVICE": base_clone_device(doc.image)},
				timeout_seconds=60,
			).stdout
		)["hydration_percent"]
	)
	stalled = percent == (doc.hydration_percent or 0)
	doc.db_set({"hydration_percent": percent, "hydration_last_polled": frappe.utils.now_datetime()})
	_progress(
		doc,
		f"Copying base image {doc.image} from {_server_title(doc.source_server)} to "
		f"{_server_title(doc.target_server)} — {percent}% hydrated.",
		percent=percent,
	)
	if percent >= 100:
		return True
	if stalled:
		ticks = (doc.hydration_stall_ticks or 0) + 1
		if ticks >= HYDRATION_STALL_TICKS:
			frappe.throw(f"base image hydration stalled at {percent}% for {ticks} ticks")
		doc.db_set("hydration_stall_ticks", ticks)
	else:
		doc.db_set("hydration_stall_ticks", 0)
	return False  # re-enter next tick


def _phase_finalizing(doc) -> bool:
	"""Target: collapse the fully-hydrated base clone to a plain, read-only local base
	LV (migration-receive-base PHASE=finalize) and extract the image-dir tar (kernel +
	rootfs sentinel). After this the base is a first-class local image on the target,
	indistinguishable from a synced one. Idempotent: the script no-ops on an already
	present + read-only base LV."""
	base_disk_gb = _bytes_to_gib_ceil(int(doc.base_size_bytes or 0))
	_run_phase_task(
		doc,
		server=doc.target_server,
		script="migration-receive-base",
		variables={
			"IMAGE_NAME": doc.image,
			"DISK_GB": str(base_disk_gb),
			"SOURCE_HOST": _server_ipv4(doc.source_server),
			"NBD_PORT": str(doc.nbd_port),
			"PHASE": "finalize",
		},
		timeout_seconds=120,
	)
	return True


def _phase_registering(doc) -> bool:
	"""Controller: register the target's local Virtual Machine Image row so the shipped
	base is selectable in the `image` field on a new VM there, exactly like a synced
	image. The bytes are already on the target (the base LV + kernel from Finalizing);
	this is the Frappe-side record that points at them.

	A base image row is fleet-wide (one row, many servers) for a synced image — a local
	image is per-server, but the row is still a single DocType record. We keep ONE row
	per image name: the existing local image row already describes the same bytes (same
	name, same kernel/rootfs filenames, same size, same build_mode). The target now
	holds those bytes too, so no new row is needed and nothing changes on the row —
	this phase is the explicit no-op place where a future per-server presence record
	would live. Kept as a phase so the status walk mirrors a migration's Repointing.

	Idempotent by construction (it reads, asserts, writes nothing)."""
	# The image row already exists (the export's source is an existing local image);
	# the target now has the bytes. Assert the row is still there so a deleted-image
	# race fails loudly rather than leaving orphaned bytes on the target.
	if not frappe.db.exists("Virtual Machine Image", doc.image):
		frappe.throw(
			f"Virtual Machine Image {doc.image} no longer exists; the bytes shipped to "
			f"{doc.target_server} are orphaned — recreate the image row or clean up the LV."
		)
	return True


def _phase_cleanup(doc) -> bool:
	"""Source: stop the NBD exports and drop the staged image-dir tar. The base LV is
	untouched (it is the source's own image, still in use there). Reuses
	migration-cleanup-base — the mirror of the export the source started. If it fails,
	the row stays at Cleanup with the error; the row IS the backstop."""
	_run_phase_task(
		doc,
		server=doc.source_server,
		script="export-cleanup-source",
		variables={
			"IMAGE_NAME": doc.image,
			"NBD_PORT": str(doc.nbd_port or 0),
		},
		timeout_seconds=60,
	)
	return True


PHASES = {
	"Pending": _phase_pending,
	"Exporting": _phase_exporting,
	"Hydrating": _phase_hydrating,
	"Finalizing": _phase_finalizing,
	"Registering": _phase_registering,
	"Cleanup": _phase_cleanup,
}


# ─────────────────────────────────────────────────────────────────────────────
# Task running + lost-task detection (mirrors migration._run_phase_task).
# ─────────────────────────────────────────────────────────────────────────────


def _run_phase_task(doc, *, server: str, script: str, variables: dict, timeout_seconds: int):
	"""Run a phase's host script inline. run_task saves the Task row first and raises
	on failure (→ caught by reconcile_image_exports → Failed). Lost-task detection
	re-enters a prior Running/Pending Task of the same script that blew its timeout
	(recorded, never a silent duplicate). An export has no VM, so the Task carries no
	`virtual_machine` link — lost-task detection keys off the script + this image in the
	Task variables instead."""
	_detect_lost_task(doc, script, timeout_seconds)
	return run_task(
		server=server,
		script=script,
		variables=variables,
		timeout_seconds=timeout_seconds,
	)


def _detect_lost_task(doc, script: str, timeout_seconds: int) -> None:
	"""If the most recent Task for this export's image+script is still Running/Pending
	well past its timeout, it's lost (the worker died mid-run). Log it and mark it
	Failure; the inline re-run that follows is safe because every phase script is
	idempotent. Matched on script + IMAGE_NAME in the Task variables — every export
	phase task carries this image name, and no VM link exists to key on."""
	rows = frappe.db.sql(
		"""
		SELECT name, creation FROM `tabTask`
		WHERE script = %(script)s
		  AND status IN ('Running', 'Pending')
		  AND variables LIKE %(pattern)s
		ORDER BY creation DESC
		LIMIT 1
		""",
		{"script": script, "pattern": f'%"IMAGE_NAME": "{doc.image}"%'},
		as_dict=True,
	)
	if not rows:
		return
	started = rows[0].creation
	if started and frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), started) > (
		LOST_TASK_TIMEOUT_FACTOR * timeout_seconds
	):
		frappe.logger("atlas").warning(
			f"image export {doc.name}: Task {rows[0].name} ({script}) appears lost; "
			f"re-entering phase idempotently"
		)
		frappe.db.set_value("Task", rows[0].name, "status", "Failure")


def _fail(name: str, message: str) -> None:
	"""Mark an export Failed, recording the phase it failed at so retry() resumes there.
	Best-effort and self-committing (it runs after a rollback)."""
	doc = frappe.get_doc("Virtual Machine Image Export", name)
	doc.db_set({"status": "Failed", "error_message": message[-2000:], "error_at_status": doc.status})
	# nosemgrep: frappe-manual-commit -- persist the failure so the next tick sees it
	frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Small read helpers.
# ─────────────────────────────────────────────────────────────────────────────


def _bytes_to_gib_ceil(size_bytes: int) -> int:
	"""Round a byte size UP to whole GiB — the target base LV must be at least the
	source's size (a smaller thin LV would truncate the copy)."""
	gib = 1024**3
	return (size_bytes + gib - 1) // gib


def _server_ipv4(server: str) -> str:
	return frappe.db.get_value("Server", server, "ipv4_address")


def _server_title(server: str) -> str:
	"""A human-readable host name for the progress line, falling back to the row name."""
	return frappe.db.get_value("Server", server, "title") or server


def _phase_label(doc, phase: str) -> str:
	source, target = _server_title(doc.source_server), _server_title(doc.target_server)
	return {
		"Pending": f"Preparing to export {doc.image} from {source}.",
		"Exporting": f"Serving base image {doc.image} over NBD from {source}.",
		"Hydrating": f"Copying base image {doc.image} from {source} to {target}.",
		"Finalizing": f"Collapsing the shipped base image to local storage on {target}.",
		"Registering": f"Registering {doc.image} as a local image on {target}.",
		"Cleanup": f"Tearing down the NBD export on {source}.",
	}.get(phase, phase)


def _progress(doc, detail: str, *, percent: int = -1) -> None:
	"""Write the always-current progress line (and, for a measurable copy, its percent)
	straight to the row via db_set so it is visible immediately — every tick, mid-phase,
	even while a long host task is still running. `percent=-1` means "not a measurable
	copy" and the form hides the bar."""
	doc.db_set({"progress_detail": detail, "progress_percent": percent})


def export_port(image: str) -> int:
	"""A stable per-image TCP port so concurrent exports of different images on one
	source host never collide. Derived from the image name (hashed), matching how
	migration.nbd_port derives a per-VM port from the UUID — a pure function, no stored
	state, so the controller and the host script agree. The image-dir tar rides on
	export_port + 1, so we stride the range by 2 to keep pairs from overlapping."""
	digest = int(_uuid.uuid5(_uuid.NAMESPACE_DNS, image).hex[:4], 16)
	return 20000 + (digest % 2500) * 2
