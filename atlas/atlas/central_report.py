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
reverted. The deliver job is enqueue_after_commit, so a rolled-back emit's job
never runs — its row stays `pending` and is never POSTed (log the attempt, skip
the delivery). On commit, deliver() POSTs and stamps the same row ok / error /
skipped. The single's `status` field stays the at-a-glance breadcrumb; the log
is the queryable history.
"""

from __future__ import annotations

import json

import frappe

from atlas.atlas.central import CentralError


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
	if _enabled() and _status_changed(doc):
		_emit("vm.status_changed", _vm_payload(doc), doc)


def on_vm_trash(doc, method=None):
	if _enabled():
		_emit("vm.deleted", _vm_payload(doc), doc)


def on_site_after_insert(doc, method=None):
	if _enabled():
		_emit("site.created", _site_payload(doc), doc)


def on_site_update(doc, method=None):
	if _enabled() and _status_changed(doc):
		_emit("site.status_changed", _site_payload(doc), doc)


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
	log_name = _write_log(event_type, payload, doc)
	frappe.enqueue(
		"atlas.atlas.central_report.deliver",
		queue="default",
		timeout=60,
		enqueue_after_commit=True,
		log_name=log_name,
		event_type=event_type,
		payload=payload,
	)


def _write_log(event_type: str, payload: dict, doc=None) -> str:
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
				"occurred_at": frappe.utils.now(),
				"reference_doctype": doc.doctype if doc is not None else None,
				"reference_name": doc.name if doc is not None else None,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def deliver(log_name: str, event_type: str, payload: dict) -> None:
	"""Background job: POST one event to Central and stamp its Central Event Log
	row with the outcome. Also updates the single's `status` breadcrumb so the
	operator sees the last delivery at a glance. Runs only on commit
	(enqueue_after_commit), so a rolled-back emit's row is never reached here and
	stays `pending` — logged, never delivered."""
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
				"occurred_at": frappe.utils.now(),
			}
		)
		_stamp(log_name, status="ok", http_status=200)
		settings.db_set("status", f"ok: {event_type}", commit=True)
	except CentralError as exception:
		frappe.log_error(f"Central event {event_type} failed: {exception}", "Central event")
		_stamp(log_name, status="error", last_error=str(exception)[:140], http_status=exception.status_code)
		settings.db_set("status", f"error: {exception}"[:140], commit=True)


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


def _vm_payload(doc) -> dict:
	# The owning Central team, so the control plane can attribute this VM to a
	# tenant. The Tenant `name` *is* the Central `Team.name`, so the VM's `tenant`
	# link is the owning team directly; None for operator-owned VMs.
	return {
		"name": doc.name,
		"team": doc.tenant or None,
		"title": doc.title,
		"status": doc.status,
		"server": doc.server,
		"size_preset": doc.get("size_preset"),
		"vcpus": doc.get("vcpus"),
		"memory_megabytes": doc.get("memory_megabytes"),
		"disk_gigabytes": doc.get("disk_gigabytes"),
		"ipv6_address": doc.get("ipv6_address"),
		"public_ipv4": doc.get("public_ipv4"),
	}


def _site_payload(doc) -> dict:
	# The owning Central team, so the control plane can attribute this site to a
	# tenant. The Tenant `name` *is* the Central `Team.name`, so the Site's `tenant`
	# link is the owning team directly; None for operator/e2e sites.
	# The login URL + live URL are the tenant handoff — only meaningful once the
	# site is serving (Running), and the field is stamped before the readiness
	# wait. Before that there is nothing to hand off.
	running = doc.status == "Running"
	return {
		"name": doc.name,
		"team": doc.tenant or None,
		"subdomain": doc.get("subdomain"),
		"status": doc.status,
		"fqdn": doc.name,
		"url": f"https://{doc.name}" if running else None,
		"login_url": doc.get("login_url") if running else None,
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
