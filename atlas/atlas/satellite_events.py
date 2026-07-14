"""Lifecycle webhooks to the Satellite orchestrator (spec/28, the provisioner/
orchestrator split).

Atlas is a pure provisioner — it never calls into a service. But a Satellite (a
SEPARATE deployment) needs to know when a VM comes and goes so it can register the
box and (de)apply its services. Atlas emits a thin, HMAC-signed webhook on the VM
lifecycle; the body carries only identity — which Atlas, which VM, what happened —
and the Satellite reads the full VM back through the read API (`api/satellite.py`).

Wired as `doc_events` observers, exactly like Central reporting (`central_report.py`),
so core stays oblivious to what the Satellite does with the event. Delivery is
best-effort and runs after commit, so a Satellite that is down never wedges a
provision; the Satellite's own reconcile sweep (`list_virtual_machines`) is the
backstop for a missed webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import frappe

DELIVER = "atlas.atlas.satellite_events.deliver"
SIGNATURE_HEADER = "X-Atlas-Signature"
TIMEOUT_SECONDS = 10


def _endpoint() -> tuple[str, str] | None:
	"""(url, secret) of the Satellite webhook, or None when unconfigured (a bare Atlas
	with no Satellite, so every emit is a no-op)."""
	settings = frappe.get_single("Atlas Settings")
	url = settings.satellite_webhook_url
	if not url:
		return None
	return url, settings.get_password("satellite_webhook_secret", raise_exception=False) or ""


def on_vm_after_insert(doc, method=None) -> None:
	_notify("vm.registered", doc.name)


def on_vm_update(doc, method=None) -> None:
	# Only meaningful lifecycle transitions — a status change is when the Satellite
	# needs to re-pull (addresses/host get populated as the VM reaches Running). Other
	# saves are ignored; the reconcile sweep covers anything missed.
	previous = doc.get_doc_before_save()
	if previous is not None and previous.status != doc.status:
		_notify("vm.updated", doc.name)


def on_vm_trash(doc, method=None) -> None:
	_notify("vm.deregistered", doc.name)


def _notify(event: str, vm: str) -> None:
	"""Enqueue a best-effort delivery, but only when a Satellite is configured. After
	commit, so a rolled-back provision never emits."""
	if _endpoint() is None:
		return
	frappe.enqueue(DELIVER, queue="short", enqueue_after_commit=True, event=event, vm=vm)


def _sign(secret: str, body: str) -> str:
	return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def deliver(event: str, vm: str) -> None:
	"""Background job: POST one signed lifecycle event to the Satellite. Best-effort —
	a failed POST is logged, never raised (the Satellite reconciles independently)."""
	import requests

	endpoint = _endpoint()
	if endpoint is None:
		return
	url, secret = endpoint
	body = json.dumps(
		{
			"atlas": frappe.utils.get_url(),
			"event": event,
			"virtual_machine": vm,
			"occurred_at": frappe.utils.now(),
		},
		sort_keys=True,
	)
	try:
		requests.post(
			url,
			data=body,
			headers={"Content-Type": "application/json", SIGNATURE_HEADER: _sign(secret, body)},
			timeout=TIMEOUT_SECONDS,
		)
	except Exception as exception:
		frappe.log_error(f"Satellite webhook {event} for {vm} failed: {exception}", "Satellite webhook")
