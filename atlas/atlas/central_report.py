"""Report resource lifecycle events to Central (spec/16-central.md § Event reporting).

Wired via doc_events in hooks.py — no controller edits. A status transition on a
Virtual Machine / Site / Virtual Machine Snapshot / Server, and a VM / Site
after_insert, enqueue a background `deliver` job that POSTs to Central. Sites are
the Central-driven self-serve surface (spec/14-self-serve.md): Central calls
`create_site`, then learns the site reached Running (with its admin handoff) from
the `site.status_changed` event here or by polling `get_site`.

Everything is gated on Central Settings.enabled, so a site without Central
configured pays nothing. Delivery is fire-and-forget: a failure is logged and
never blocks the VM operation (the spec's accepted v1 tradeoff; a durable outbox
is the documented upgrade). enqueue_after_commit ensures a rolled-back
transaction is never *delivered* to Central.

Every emit also writes a Central Event Log row — a MyISAM (non-transactional)
audit trail. Because MyISAM rows are not enrolled in the request transaction,
the row survives a rollback of the business change that triggered it: you can
always see what we *tried* to emit, even for a VM/Site save that was later
reverted. On commit the row is marked `queued` and the deliver job is enqueued;
on rollback it is marked `rolled_back` and never POSTed. The queued row is the
durable outbox: if RQ misses the immediate job, the scheduler can safely replay
only committed events. deliver() POSTs and stamps the same row ok / error /
skipped. The single's `status` field stays the at-a-glance breadcrumb; the log is
the queryable history.
"""

from __future__ import annotations

import json

import frappe

from atlas.atlas.central import CentralError

MAX_PENDING_RETRY_BATCH = 100


def _enabled() -> bool:
	# get_single_value tolerates the single's row not existing yet (fresh site).
	return bool(frappe.db.get_single_value("Central Settings", "enabled"))


def _status_changed(doc) -> bool:
	"""True when this save flips `status`. before is None on insert."""
	before = doc.get_doc_before_save()
	return before is None or before.status != doc.status


# --- doc_events handlers ---------------------------------------------------


def on_vm_after_insert(doc, method=None):
	if _enabled():
		_emit("vm.created", _vm_payload(doc), doc)


def on_vm_update(doc, method=None):
	if not _enabled():
		return
	if _status_changed(doc):
		_emit("vm.status_changed", _vm_payload(doc), doc)
	elif doc.flags.get("resizing"):
		# A resize rewrites the machine's shape (vcpus/memory/disk) but leaves the VM
		# Stopped, so no status_changed fires — emit an explicit resized event so
		# Central's mirror picks up the new shape instead of silently drifting.
		_emit("vm.resized", _vm_payload(doc), doc)


def on_vm_trash(doc, method=None):
	if _enabled():
		_emit("vm.deleted", _vm_payload(doc), doc)


def on_pilot_update(doc, method=None):
	# A Pilot reports AS its backing VM (Central mirrors VMs, not Pilots): a Pilot
	# status change is emitted as a vm.status_changed carrying the VM-shaped payload
	# — this is the event that delivers the login handoff, since the Pilot flips
	# Running only after the in-guest mint. A Pilot with no VM yet (created but its
	# after_insert hasn't linked one) has nothing to report.
	if _enabled() and _status_changed(doc) and doc.virtual_machine:
		report_pilot_status(doc)


def report_pilot_status(pilot) -> None:
	"""Emit a Pilot's status as a vm.status_changed, carrying the login handoff.

	The `on_update` doc_event above delivers this for a plain `.save()`. But the
	terminal Running flip in `Pilot.auto_provision` is a `db_set` (skips validation
	mid-job), and `db_set` runs only `on_change`, never `on_update` — so that flip,
	the very event that carries the freshly-minted login_url, would otherwise never
	push. auto_provision calls this explicitly after its commit to close that gap;
	the periodic reconcile is only the backstop, not the primary delivery."""
	if _enabled() and pilot.virtual_machine:
		_emit("vm.status_changed", _pilot_vm_payload(pilot), pilot)


def on_site_after_insert(doc, method=None):
	if _enabled():
		_emit("site.created", _site_payload(doc), doc)


def on_site_update(doc, method=None):
	if _enabled() and _status_changed(doc):
		_emit("site.status_changed", _site_payload(doc), doc)


def report_site_status(site) -> None:
	"""Emit a Site's current status as a site.status_changed event.

	The `on_update` doc_event above delivers this for a plain `.save()`. But every
	real lifecycle transition in `Site.auto_provision` (Provisioning → Deploying →
	Running / Failed) goes through `_set_status`, which uses `db_set` — and `db_set`
	runs only `on_change`, never `on_update`, so those transitions would never push.
	auto_provision's `_set_status` calls this explicitly (before its commit) to close
	that gap; without it Central's mirror only ever sees the initial Pending
	(site.created + the insert's on_update) and the site stays stuck at Pending —
	there is no site reconcile pull to correct it. Same shape as report_pilot_status."""
	if _enabled():
		_emit("site.status_changed", _site_payload(site), site)


def on_snapshot_update(doc, method=None):
	if _enabled() and _status_changed(doc) and doc.status == "Available":
		_emit("snapshot.completed", _snapshot_payload(doc), doc)


def on_server_update(doc, method=None):
	if _enabled() and _status_changed(doc):
		_emit("server.status_changed", _server_payload(doc), doc)


# --- delivery --------------------------------------------------------------


def _emit(event_type: str, payload: dict, doc=None) -> None:
	# Write the audit row FIRST, then enqueue delivery against it. The row is the
	# durable record of the attempt; the deliver job (after-commit) only stamps it.
	occurred_at = frappe.utils.now()
	log_name = _write_log(event_type, payload, doc, occurred_at=occurred_at)
	_mark_log_after_transaction(log_name)
	_enqueue_delivery_after_commit(log_name, event_type, payload, occurred_at)


def _mark_log_after_transaction(log_name: str) -> None:
	frappe.db.after_commit.add(lambda: _set_log_status(log_name, "queued"))
	frappe.db.after_rollback.add(lambda: _set_log_status(log_name, "rolled_back"))


def _enqueue_delivery_after_commit(log_name: str, event_type: str, payload: dict, occurred_at: str) -> None:
	frappe.enqueue(
		"atlas.atlas.central_report.deliver",
		queue="default",
		timeout=60,
		enqueue_after_commit=True,
		log_name=log_name,
		event_type=event_type,
		payload=payload,
		occurred_at=occurred_at,
	)


def retry_pending(limit: int = 50, include_legacy_pending: bool = False) -> int:
	"""Drain committed-but-undelivered Central events in creation order.

	The Central Event Log is the durable outbox. A missed RQ job must not strand the
	mirror forever; retrying queued rows replays the same delivery contract. Legacy
	pending rows from builds before `queued` existed can be replayed manually, but
	the scheduler must not touch fresh pending rows because their source transaction
	may still roll back."""
	if not _delivery_ready():
		return 0

	statuses = ["queued"]
	if include_legacy_pending:
		statuses.append("pending")

	limit = _retry_limit(limit)
	rows = frappe.get_all(
		"Central Event Log",
		filters={"status": ["in", statuses]},
		fields=["name", "event_type", "payload", "occurred_at"],
		order_by="creation asc",
		limit=limit,
	)
	if not rows:
		return 0
	for row in rows:
		deliver(row.name, row.event_type, json.loads(row.payload or "{}"), occurred_at=row.occurred_at)
	return len(rows)


def _delivery_ready() -> bool:
	settings = frappe.get_single("Central Settings")
	return bool(settings.enabled and settings.api_key)


def _retry_limit(limit: int) -> int:
	try:
		value = int(limit)
	except (TypeError, ValueError):
		value = 50
	return max(1, min(value, MAX_PENDING_RETRY_BATCH))


def _write_log(event_type: str, payload: dict, doc=None, occurred_at: str | None = None) -> str:
	"""Insert the Central Event Log row for this emit and return its name.

	The Central Event Log is MyISAM (non-transactional), so this INSERT hits the
	table immediately and is NOT rolled back if the surrounding business
	transaction is — we always keep a record of what we tried to emit. We
	deliberately do NOT frappe.db.commit() here: we run inside a doc_event
	mid-transaction, and committing would flush the *outer* (InnoDB) business
	change early, breaking the rollback guarantee. MyISAM needs no commit for the
	row to be durable."""
	return (
		frappe.get_doc(
			{
				"doctype": "Central Event Log",
				"event_type": event_type,
				"payload": json.dumps(payload, default=str, indent=2),
				"status": "pending",
				"attempts": 0,
				"occurred_at": occurred_at or frappe.utils.now(),
				"reference_doctype": doc.doctype if doc is not None else None,
				"reference_name": doc.name if doc is not None else None,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def deliver(log_name: str, event_type: str, payload: dict, occurred_at: str | None = None) -> None:
	"""Background job: POST one event to Central and stamp its Central Event Log
	row with the outcome. Also updates the single's `status` breadcrumb so the
	operator sees the last delivery at a glance. Runs only on commit
	(enqueue_after_commit), so a rolled-back emit's row is never reached here and is
	stamped `rolled_back` — logged, never delivered."""
	settings = frappe.get_single("Central Settings")
	if not settings.enabled:
		return
	if not settings.api_key:
		# Enabled but not yet registered: without the scoped service-user creds we
		# can't authenticate to Central (and the sender is resolved from that identity),
		# so skip rather than POST unauthenticated. Register first.
		_stamp(log_name, status="skipped")
		settings.db_set("status", "skipped: register with Central first", commit=True)
		return
	try:
		settings.client().post_event(
			{
				"type": event_type,
				"payload": payload,
				"occurred_at": _iso(occurred_at) or frappe.utils.now(),
			}
		)
		_stamp(log_name, status="ok", http_status=200)
		settings.db_set("status", f"ok: {event_type}", commit=True)
	except CentralError as exception:
		frappe.log_error(f"Central event {event_type} failed: {exception}", "Central event")
		_stamp(log_name, status="error", last_error=str(exception)[:140], http_status=exception.status_code)
		settings.db_set("status", f"error: {exception}"[:140], commit=True)


def _set_log_status(log_name: str, status: str) -> None:
	try:
		frappe.db.set_value("Central Event Log", log_name, "status", status, update_modified=False)
	except Exception:
		frappe.log_error(f"Central Event Log {log_name} {status} stamp failed", "Central event")


def _stamp(
	log_name: str, *, status: str, last_error: str | None = None, http_status: int | None = None
) -> None:
	"""Record a delivery outcome on the event-log row. Best-effort: the row is a
	MyISAM audit breadcrumb, so a stamp failure must never sink the deliver job (or
	mask the real Central error). bump attempts so a delivered row is visibly != pending."""
	try:
		log = frappe.get_doc("Central Event Log", log_name)
		log.status = status
		log.attempts = (log.attempts or 0) + 1
		if last_error is not None:
			log.last_error = last_error
		if http_status is not None:
			log.http_status = http_status
		log.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(f"Central Event Log {log_name} stamp failed", "Central event")


# --- payloads --------------------------------------------------------------
# Subsets mirroring Task._publish_update's shape: identity + the fields Central
# needs to reflect fleet state, not the whole document.


def _iso(value):
	"""Render a datetime field as an ISO-8601 string so the payload is JSON-native.
	`doc.get` on a Datetime field hands back a live `datetime`, which requests'
	stdlib json.dumps (no default=str) can't serialize — the POST would crash on
	the delivery job. Tolerates a str (already rendered) or None untouched."""
	return value.isoformat(sep=" ") if hasattr(value, "isoformat") else value


def _vm_payload(doc) -> dict:
	# The owning Central team, so the control plane can attribute this VM to a
	# tenant. The Tenant `name` *is* the Central `Team.name`, so the VM's `tenant`
	# link is the owning team directly; None for operator-owned VMs.
	#
	# A bench/site VM is owned by a Pilot OR a Site (the front door lives there, not on
	# the VM), so the front-door fields are read THROUGH whichever aggregate backs this
	# VM: gateway_url (`https://<fqdn>`) and, once it is Running, login_url + its expiry.
	# A plain VM (proxy, operator machine) has neither → those stay None. The login
	# handoff arrives on the aggregate's own status_changed event (_pilot_vm_payload for
	# a Pilot, the site.* events for a Site); a plain VM lifecycle event just carries the
	# (stable) gateway_url. front_door_for_vm resolves either aggregate — a Site-backed
	# VM (create_site) is no longer login-less on the Asset (spec/14-self-serve.md).
	from atlas.atlas.front_door import front_door_for_vm

	return _merge_bench_fields(
		{
			"name": doc.name,
			"team": doc.tenant or None,
			"title": doc.title,
			"status": doc.status,
			"server": doc.server,
			"pilot_credential_id": doc.get("pilot_credential_id"),
			"size_preset": doc.get("size_preset"),
			"vcpus": doc.get("vcpus"),
			"memory_megabytes": doc.get("memory_megabytes"),
			"disk_gigabytes": doc.get("disk_gigabytes"),
			"ipv6_address": doc.get("ipv6_address"),
			"public_ipv4": doc.get("public_ipv4"),
		},
		front_door_for_vm(doc.name),
	)


def _pilot_vm_payload(pilot) -> dict:
	# The VM-shaped payload for a Pilot's own lifecycle event (and its regenerate
	# return). Central mirrors VMs, so a Pilot reports AS its backing VM: plain VM
	# facts are read through the `virtual_machine` link, the bench fields off the
	# Pilot. This is the event that carries the login handoff (the Pilot flips Running
	# only after the mint), so its status is the PILOT's — the VM booted earlier.
	from atlas.atlas.front_door import FrontDoor

	vm = frappe.get_doc("Virtual Machine", pilot.virtual_machine)
	return _merge_bench_fields(
		{
			"name": vm.name,
			"team": pilot.tenant or None,
			"title": vm.title,
			"status": pilot.status,
			"server": vm.server,
			"pilot_credential_id": vm.get("pilot_credential_id"),
			"size_preset": vm.get("size_preset"),
			"vcpus": vm.get("vcpus"),
			"memory_megabytes": vm.get("memory_megabytes"),
			"disk_gigabytes": vm.get("disk_gigabytes"),
			"ipv6_address": vm.get("ipv6_address"),
			"public_ipv4": vm.get("public_ipv4"),
		},
		FrontDoor(pilot),
	)


def _merge_bench_fields(payload: dict, front_door) -> dict:
	"""Fold a front door's (Pilot or Site) fields onto a VM-shaped payload. gateway_url
	is the derived FQDN URL (stable once the aggregate exists); login_url + its expiry
	are the one-click handoff, meaningful only once it is Running (before that the mint
	hasn't run — FrontDoor gates them). A None front_door (a plain, non-bench VM) leaves
	all three None."""
	payload["gateway_url"] = front_door.gateway_url if front_door is not None else None
	payload["login_url"] = front_door.login_url if front_door is not None else None
	payload["login_url_expires_at"] = (
		_iso(front_door.login_url_expires_at) if front_door is not None else None
	)
	return payload


def _site_payload(doc) -> dict:
	# The owning Central team, so the control plane can attribute this site to a
	# tenant. The Tenant `name` *is* the Central `Team.name`, so the Site's `tenant`
	# link is the owning team directly; None for operator/e2e sites.
	# The login URL + its expiry + live URL are the tenant handoff — only
	# meaningful once the site is serving (Running), and the fields are stamped
	# before the readiness wait. login_url_expires_at is when the URL stops working
	# (mint time + the `bench browse --sid` session's 24h TTL), so Central compares
	# against it and regenerates a fresh one for a late click. Before Running there
	# is nothing to hand off.
	running = doc.status == "Running"
	return {
		"name": doc.name,
		"team": doc.tenant or None,
		"subdomain": doc.get("subdomain"),
		"status": doc.status,
		"fqdn": doc.name,
		"url": f"https://{doc.name}" if running else None,
		"login_url": doc.get("login_url") if running else None,
		"login_url_expires_at": _iso(doc.get("login_url_expires_at")) if running else None,
	}


def _snapshot_payload(doc) -> dict:
	return {
		"name": doc.name,
		"status": doc.status,
		"virtual_machine": doc.get("virtual_machine"),
		"server": doc.get("server"),
		"kind": doc.get("kind"),
	}


def _server_payload(doc) -> dict:
	return {"name": doc.name, "status": doc.status}
