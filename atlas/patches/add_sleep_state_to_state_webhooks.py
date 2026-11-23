import json

import frappe

from atlas.vm.core.state_webhook import DELIVERIES


def execute() -> None:
	"""Send the sleep state in the state deliveries that Central already configured."""
	for name in frappe.get_all(
		"Webhook", filters={"name": ["like", "Virtual Machine State - % - Central - %"]}, pluck="name"
	):
		webhook = frappe.get_doc("Webhook", name)
		body = json.loads(webhook.webhook_json)
		body["sleep_state"] = "{{ doc.sleep_state }}"
		webhook.webhook_json = frappe.as_json(body)
		webhook.condition = DELIVERIES[webhook.webhook_docevent][1]
		webhook.save()
