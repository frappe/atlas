"""Report VM lifecycle events to Central (spec/16-central.md § Event reporting).

Wired via doc_events in hooks.py — no controller edits. A status transition on a
Virtual Machine / Virtual Machine Snapshot / Server, and a VM after_insert,
enqueue a background `deliver` job that POSTs to Central.

Everything is gated on Central Settings.enabled, so a site without Central
configured pays nothing. Delivery is fire-and-forget: a failure is logged and
never blocks the VM operation (the spec's accepted v1 tradeoff; a durable outbox
is the documented upgrade). enqueue_after_commit ensures a rolled-back
transaction is never reported.
"""

from __future__ import annotations

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
		_emit("vm.created", _vm_payload(doc))


def on_vm_update(doc, method=None):
	if _enabled() and _status_changed(doc):
		_emit("vm.status_changed", _vm_payload(doc))


def on_vm_trash(doc, method=None):
	if _enabled():
		_emit("vm.deleted", _vm_payload(doc))


def on_snapshot_update(doc, method=None):
	if _enabled() and _status_changed(doc) and doc.status == "Available":
		_emit("snapshot.completed", _snapshot_payload(doc))


def on_server_update(doc, method=None):
	if _enabled() and _status_changed(doc):
		_emit("server.status_changed", _server_payload(doc))


# --- delivery --------------------------------------------------------------


def _emit(event_type: str, payload: dict) -> None:
	frappe.enqueue(
		"atlas.atlas.central_report.deliver",
		queue="default",
		timeout=60,
		enqueue_after_commit=True,
		event_type=event_type,
		payload=payload,
	)


def deliver(event_type: str, payload: dict) -> None:
	"""Background job: POST one event to Central. Records the outcome on the
	single so the operator sees the last delivery at a glance."""
	settings = frappe.get_single("Central Settings")
	if not settings.enabled:
		return
	try:
		settings.client().post_event(
			{
				"atlas_id": settings.atlas_id,
				"type": event_type,
				"payload": payload,
				"occurred_at": frappe.utils.now(),
			}
		)
		settings.db_set("last_event_status", f"ok: {event_type}", commit=True)
	except CentralError as exception:
		frappe.log_error(f"Central event {event_type} failed: {exception}", "Central event")
		settings.db_set("last_event_status", f"error: {exception}"[:140], commit=True)


# --- payloads --------------------------------------------------------------
# Subsets mirroring Task._publish_update's shape: identity + the fields Central
# needs to reflect fleet state, not the whole document.


def _vm_payload(doc) -> dict:
	return {
		"name": doc.name,
		"status": doc.status,
		"server": doc.server,
		"size_preset": doc.get("size_preset"),
		"vcpus": doc.get("vcpus"),
		"memory_megabytes": doc.get("memory_megabytes"),
		"disk_gigabytes": doc.get("disk_gigabytes"),
		"ipv6_address": doc.get("ipv6_address"),
		"public_ipv4": doc.get("public_ipv4"),
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
