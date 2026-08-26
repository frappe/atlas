"""Shared plumbing for the resumable phase machines (VM migration, base-image export).

Both are the same shape (spec/24-vm-migration.md): a row walks an ordered list of
idempotent host phases, each recorded as its `status`; a self-drive job advances one
phase then re-enqueues itself, and a `*/2` cron re-enters the recorded phase as a pure
SAFETY NET so a dropped RQ job never strands the operation. Every phase resumes from the
DB, never from in-memory state, so a crash mid-phase costs at most a cheap re-entry.

This module holds the parts that are IDENTICAL between the two machines — the
reconcile/self-drive/advance driver and the progress/fail/server-lookup helpers — so
`migration.py` and `export.py` carry only what actually differs: their phase list, phase
handlers, per-phase labels, and how a phase's host work reaches the host (its
`_run_phase_task`). A machine describes itself once as a `ResumableOperation` and calls
`reconcile_all`/`self_drive`/`advance` with it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import frappe

# A phase Task stuck Running/Pending past this multiple of its timeout is treated as
# lost (the worker died mid-run) and the phase is re-entered idempotently.
LOST_TASK_TIMEOUT_FACTOR = 2

# How many consecutive no-progress hydration polls before a machine gives up.
HYDRATION_STALL_TICKS = 30


@dataclass(frozen=True)
class ResumableOperation:
	"""A resumable phase machine's fixed description — the knobs the shared driver needs
	to advance one of its rows. Built once at each machine's module load."""

	doctype: str  # the row's DocType
	driver: str  # dotted path of the self-drive entrypoint (re-enqueued by self_drive)
	queue_timeout: int  # RQ timeout for the self-drive job (bounds the longest phase)
	phase_order: tuple[str, ...]  # ordered phases; the last entry is the terminal "Done"
	phases: dict[str, Callable]  # phase name → handler(doc) -> True (advance) / False (hold)
	label: Callable  # (doc, phase) -> the human-readable progress line stamped before a phase
	complete_detail: str  # progress_detail stamped when the row reaches Done
	log_label: str  # noun for the failure log line, e.g. "migration" / "image export"


def reconcile_all(op: ResumableOperation) -> None:
	"""Scheduler safety-net entry. Advance every non-terminal row one step. Try/except PER
	ROW (in reconcile_one): one wedged operation never blocks the others, and a terminal
	failure marks only its own row Failed. Re-entrant by construction — if the previous
	tick crashed mid-phase, this tick re-enters the same phase (idempotent)."""
	names = frappe.get_all(op.doctype, filters={"status": ["not in", ("Done", "Failed")]}, pluck="name")
	for name in names:
		reconcile_one(op, name)


def self_drive(op: ResumableOperation, name: str) -> None:
	"""The row's OWN driver: advance one phase (or run one holding poll), then re-enqueue
	itself for the next step until the row is terminal — self-pacing the long copy on the
	inline poll's round-trip, with no wait for a cron tick between steps. The insert-time
	kick enqueues the first call; every step chains the next. The cron is then a pure
	safety net for a dropped self-drive job (a worker crash, an OOM kill)."""
	if not frappe.db.exists(op.doctype, name):
		return
	reconcile_one(op, name)
	# reconcile_one already committed the new status (or marked the row Failed); re-read it
	# to decide whether there is another step to drive.
	status = frappe.db.get_value(op.doctype, name, "status")
	if status not in ("Done", "Failed"):
		frappe.enqueue(op.driver, queue="long", timeout=op.queue_timeout, name=name)


def reconcile_one(op: ResumableOperation, name: str) -> bool:
	"""Advance one row a single phase, committing its progress on success and marking it
	Failed on error — in isolation, so one wedged row never blocks or rolls back another.
	Shared by the cron and the on-insert kick. Returns True iff the row advanced to a
	further non-terminal phase (more work to run immediately)."""
	try:
		advanced = advance(op, frappe.get_doc(op.doctype, name))
		# nosemgrep: frappe-manual-commit -- persist each row's progress independently so
		# one row's later failure can't roll back another's
		frappe.db.commit()
		return advanced
	except Exception as exception:
		frappe.db.rollback()
		fail(op, name, str(exception))
		frappe.logger("atlas").error(f"{op.log_label} {name} failed: {exception}")
		return False


def advance(op: ResumableOperation, doc) -> bool:
	"""Run the phase recorded on the row, then advance the status on success. Returns True
	iff the row advanced to a further NON-terminal phase — there is more work to run
	immediately (drive the next phase now rather than wait for a tick). Returns False when
	the phase held (a Hydrating poll) or reached the terminal phase (Done).

	Resumability: we ALWAYS re-derive what to do from `doc.status`, never from a cursor
	carried in. Each phase first checks its resume key, so a re-entry after a crash is a
	cheap no-op up to where it got. The live progress line is stamped BEFORE the phase
	runs, so the form shows what is happening the moment work starts — not only after a
	(possibly multi-minute) host task returns; long phases refine it as they go."""
	phase = doc.status
	if phase not in op.phase_order or phase == "Done":
		return False
	progress(doc, op.label(doc, phase), percent=-1)
	completed = op.phases[phase](doc)
	if not completed:
		return False
	nxt = op.phase_order[op.phase_order.index(phase) + 1]
	updates = {"status": nxt, "progress_percent": -1}
	if nxt == "Done":
		updates["completed_at"] = frappe.utils.now_datetime()
		updates["progress_detail"] = op.complete_detail
	doc.db_set(updates)
	return nxt != "Done"


def fail(op: ResumableOperation, name: str, message: str) -> None:
	"""Mark a row Failed, recording the phase it failed at (error_at_status) so retry()
	resumes there. Best-effort and self-committing (it runs after a rollback)."""
	doc = frappe.get_doc(op.doctype, name)
	doc.db_set({"status": "Failed", "error_message": message[-2000:], "error_at_status": doc.status})
	# nosemgrep: frappe-manual-commit -- persist the failure so the next tick sees it
	frappe.db.commit()


def server_ipv4(server: str) -> str:
	return frappe.db.get_value("Server", server, "ipv4_address")


def server_title(server: str) -> str:
	"""A human-readable host name for the progress line (the Server's title, e.g.
	`f1-aditya-blr3`), falling back to the row name if a title isn't set."""
	return frappe.db.get_value("Server", server, "title") or server


def progress(doc, detail: str, *, percent: int = -1) -> None:
	"""Write the always-current progress line (and, for a measurable copy, its percent)
	straight to the row via db_set so it is visible immediately — every tick, mid-phase,
	even while a long host task is still running. `percent=-1` means "not a measurable
	copy" and the form hides the bar."""
	doc.db_set({"progress_detail": detail, "progress_percent": percent})
