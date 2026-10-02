from __future__ import annotations

import frappe

REQUEST_TIMEOUT_SECONDS = 10
MAXIMUM_RETRIES = 3

DELIVERIES = {
	"on_update": (
		"vm.state",
		'doc.is_new() or doc.has_value_changed("status") or doc.has_value_changed("server")',
	),
	"on_trash": ("vm.state.deleted", None),
}


def configure_state_webhooks(
	request_url: str,
	webhook_secret: str,
	central_id: int = 1,
	enabled: bool = True,
) -> list[str]:
	"""Point the Virtual Machine State deliveries of one Central at request_url."""
	with frappe.db.advisory_lock("configure_state_webhooks"):
		region_name = frappe.get_cached_value("Atlas Settings", "Atlas Settings", "region_name")
		names = []
		for document_event, (event_name, condition) in DELIVERIES.items():
			event_label = document_event.replace("_", " ").title()
			name = f"Virtual Machine State - {event_label} - Central - {central_id}"
			if frappe.db.exists("Webhook", name):
				webhook = frappe.get_doc("Webhook", name)
			else:
				webhook = frappe.new_doc("Webhook")

			webhook.name = name
			webhook.webhook_doctype = "Virtual Machine State"
			webhook.webhook_docevent = document_event
			webhook.condition = condition
			webhook.request_url = request_url
			webhook.is_dynamic_url = 0
			webhook.background_jobs_queue = None
			webhook.request_method = "POST"
			webhook.request_structure = "JSON"
			webhook.webhook_json = frappe.as_json(
				{
					"event": event_name,
					"virtual_machine": "{{ doc.virtual_machine }}",
					"server": "{{ doc.server }}",
					"status": "{{ doc.status }}",
					"observed_at": "{{ doc.synced_at }}",
				}
			)
			webhook.enable_security = 1
			webhook.webhook_secret = webhook_secret
			webhook.timeout = REQUEST_TIMEOUT_SECONDS
			webhook.max_retries = MAXIMUM_RETRIES
			webhook.enabled = int(enabled)

			headers = [
				{"key": "Content-Type", "value": "application/json"},
				{"key": "X-FC-Source", "value": "atlas"},
			]
			if region_name:
				headers.append({"key": "X-FC-Region", "value": region_name})
			webhook.set("webhook_headers", headers)
			webhook.save(ignore_permissions=True)
			names.append(name)

	return names
