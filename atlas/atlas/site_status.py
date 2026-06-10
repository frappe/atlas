"""The user-facing provisioning step view for a `Site` (plan 04 SPA work).

`Site.status` is the controller's coarse lifecycle state (Pending → Provisioning
→ Deploying → Running, or Failed). The status page the verified user lands on
wants something finer: the *six* steps `auto_provision` runs (the spec's step
table), each shown as done / running / pending / failed, so the user watches the
work happen instead of staring at one word.

This module is the single source of truth that turns a `status` into that step
list — shared by the page's first render (`site_status.py`) and the realtime
payload the controller pushes on every transition (`site.auto_provision`). One
function, no duplication: the page and the live update can never disagree.

The mapping is intentionally derived from `status` alone (not from a per-step
progress field on the Site) — the controller already owns `status` as the one
durable state, and the six steps are a fixed, ordered consequence of it. A step
is `done` if status is past its phase, `running` if status is at its phase, and
`pending` otherwise; on `Failed` the step that the current phase maps to is
marked `failed` and the rest stay pending.
"""

from __future__ import annotations

import frappe
from frappe.utils import get_datetime

# The ordered provisioning steps, mirroring spec/14-self-serve.md's auto_provision
# table. `phase` is the `Site.status` value reached *when this step completes* —
# so the step is "running" while status sits at the PREVIOUS phase. The labels are
# user-facing (no "VM", no "SSH" jargon): the audience is a signup, not an operator.
STEPS = (
	{"key": "provision", "phase": "Provisioning", "label": "Preparing your server"},
	{"key": "boot", "phase": "Provisioning", "label": "Booting the machine"},
	{"key": "deploy", "phase": "Deploying", "label": "Installing your Frappe site"},
	{"key": "serve", "phase": "Deploying", "label": "Waiting for the site to respond"},
	{"key": "route", "phase": "Running", "label": "Putting it on the internet"},
	{"key": "ready", "phase": "Running", "label": "Your site is live"},
)

# The ordered phases `Site.status` moves through. A step's state is decided by
# where its `phase` sits relative to the current status: a step in a *past* phase
# is done, a step in the *current* phase is running (so BOTH provision steps show
# active during the long Provisioning wait, not just the first), a step in a
# *future* phase is pending. Running/Terminated are past every phase (all done).
_PHASE_ORDER = ("Pending", "Provisioning", "Deploying", "Running")

# Where Failed leaves the cursor. The controller overwrites `status` with "Failed",
# so the exact phase is lost; deploy is the common failure point (the host steps
# that SSH and run new-site), so we mark the deploy phase failed — past steps done,
# this phase failed, later steps pending. Good enough for a human-readable signal.
_FAILED_PHASE = "Deploying"


def _phase_index(status: str | None) -> int:
	"""Position of `status` in the phase order; -1 (before everything) if unknown.
	Running/Terminated sit past the last phase so every step reads done."""
	if status in ("Running", "Terminated"):
		return len(_PHASE_ORDER)
	try:
		return _PHASE_ORDER.index(status)
	except ValueError:
		return 0  # unknown/Pending-ish: nothing done yet


def steps_for(status: str | None) -> list[dict]:
	"""The six provisioning steps for a Site `status`, each tagged with `state`
	(done / running / pending / failed). Drives the checklist on the status page.

	`Running` marks every step done. `Failed` marks every step in the deploy phase
	`failed` (earlier phases done, later pending) — so the user sees roughly where
	it broke, not a bare "Failed". An unknown status degrades gracefully rather
	than throwing (this renders on a public-ish page; never 500 it)."""
	failed = status == "Failed"
	cursor = _PHASE_ORDER.index(_FAILED_PHASE) if failed else _phase_index(status)

	out = []
	for step in STEPS:
		step_phase = _PHASE_ORDER.index(step["phase"])
		if step_phase < cursor:
			state = "done"
		elif step_phase == cursor:
			state = "failed" if failed else "running"
		else:
			state = "pending"
		out.append({"key": step["key"], "label": step["label"], "state": state})
	return out


# The three *timed* phases — the merge of the six checklist steps into the only
# boundaries the controller actually records a clock for (`_set_status` stamps
# Site.<phase>_started on each transition). Each phase owns the consecutive steps
# that run inside it (so "Preparing"+"Booting" share one Provisioning timer), and
# names the Site field marking its start and the field marking its end (the next
# phase's start). Pending has no timer — it's the gap before work begins.
PHASES = (
	{
		"key": "provisioning",
		"label": "Setting up your server",
		"steps": ("provision", "boot"),
		"started_field": "provisioning_started",
		"ended_field": "deploying_started",
	},
	{
		"key": "deploying",
		"label": "Installing your Frappe site",
		"steps": ("deploy", "serve"),
		"started_field": "deploying_started",
		"ended_field": "running_started",
	},
	{
		"key": "running",
		"label": "Putting it on the internet",
		"steps": ("route", "ready"),
		"started_field": "running_started",
		# No "next" stamp — Running is the end of the timed work. The phase is
		# instantaneous from the page's view (the route + live flip happen in the
		# same _set_status), so its end IS its start: a near-zero duration.
		"ended_field": "running_started",
	},
)


def _phase_state(step_states: list[str]) -> str:
	"""Roll the (already-computed) member step states up into one phase state.
	failed wins, then running, then done if all done, else pending — so a phase
	reads 'running' the moment any step inside it is in flight."""
	if "failed" in step_states:
		return "failed"
	if "running" in step_states:
		return "running"
	if all(s == "done" for s in step_states):
		return "done"
	return "pending"


def _duration_seconds(start, end) -> float | None:
	"""Whole-ish seconds between two datetimes, or None if `start` is missing
	(the phase never began). Negative deltas (clock skew, same-instant stamps)
	clamp to 0 — a duration is never shown as below zero."""
	if not start:
		return None
	delta = (get_datetime(end) - get_datetime(start)).total_seconds()
	return max(0.0, delta)


def phases_for(site) -> list[dict]:
	"""The three timed phases for a Site, each with its rolled-up `state` and the
	`seconds` it took (or is taking). This is the "time taken by each step, merged"
	view: the six checklist steps collapse onto the three phases the controller
	actually clocks.

	Timing rules, all derived from the durable `*_started` stamps (`_set_status`):
	  - a *finished* phase: end stamp minus start stamp.
	  - the *in-flight* phase (started, not yet ended): now minus start stamp, so
	    the live page counts up.
	  - a *not-yet-started* phase: `seconds` is None (nothing to show).
	  - on Failed, the phase that broke has a start but no end → it reads its
	    elapsed-until-now; later phases stay None.
	Never throws — renders on a public-ish page."""
	step_state = {s["key"]: s["state"] for s in steps_for(site.status)}
	now = frappe.utils.now_datetime()

	out = []
	for phase in PHASES:
		state = _phase_state([step_state[k] for k in phase["steps"]])
		start = site.get(phase["started_field"])
		# A finished phase ends at its successor's stamp; an in-flight one is still
		# running, so it's measured to "now". Pending phases have no start at all.
		end = site.get(phase["ended_field"]) or now
		out.append(
			{
				"key": phase["key"],
				"label": phase["label"],
				"steps": list(phase["steps"]),
				"state": state,
				"seconds": _duration_seconds(start, end),
			}
		)
	return out


def progress_payload(site) -> dict:
	"""The realtime message the controller pushes on every status transition, and
	the same shape the page reads on first render. `site` is a Site document.

	Carries the coarse `status` (so the page can swap its overall heading + show
	the credentials card on Running), the derived `steps` (the six-item checklist),
	and `phases` (those steps merged onto the three timed phases, each with the
	seconds it took — `phases_for`). Deliberately small — no admin password here
	(that is fetched on demand, gated on Running), only what the live view needs."""
	return {
		"name": site.name,
		"subdomain": site.subdomain,
		"status": site.status,
		"steps": steps_for(site.status),
		# The merged, timed view: the six steps collapsed onto the three phases the
		# controller actually clocks, each with the seconds it took / is taking.
		"phases": phases_for(site),
	}


@frappe.whitelist()
def progress(site: str) -> dict:
	"""Polling fallback for the status page: the current step payload for a Site
	the caller owns. Realtime is the primary push (`Site.auto_provision`); this is
	the safety net the page hits on a slow interval so the view self-heals if a
	socket event was dropped or the socket never connected.

	Owner-gated like the page (`has_permission`): a user polls only their own
	site, an operator any. Throws PermissionError otherwise — the page treats a
	failed poll as transient and keeps its last good state."""
	doc = frappe.get_doc("Site", site)
	if not doc.has_permission("read"):
		raise frappe.PermissionError
	return progress_payload(doc)
