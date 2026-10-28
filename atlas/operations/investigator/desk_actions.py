from __future__ import annotations

import inspect
from typing import Any, Literal

import frappe
from frappe import _
from frappe.model.base_document import get_controller
from frappe.model.document import Document

# The Desk actions that an agent may run. Normal ones are the Actions and Diagnostics buttons of each form:
# they run without approval, except on a VM of a customer tenant. Dangerous ones always need approval.
# Every other method is blocked, such as terminate, archive, deletion, and methods that return a secret.
ALLOWED_ACTIONS: dict[Literal["normal", "dangerous"], dict[str, set[str]]] = {
	"normal": {
		"Metal Server": {
			"setup_server", "ping_server", "sync_disks", "sync_state", "configure_wireguard", "install_metald",
			"install_host_firewall", "renew_tls_certificate", "configure_telemetry", "poweron_server",
			"resize_volume", "fetch_metald_journal", "check_health",
		},
		"Virtual Machine": {
			"start", "stop", "reboot", "pause", "resume", "create_machine_image", "resize_disk",
			"update_disk_limits", "resize", "replace_ssh_keys", "replace_metadata", "update_network_throughput",
			"update_firewall", "attach_public_ip", "detach_public_ip", "set_wireguard_gateway_access", "set_routes",
			"read_routes", "fetch_firecracker_log", "fetch_journal",
		},
		"Virtual Machine Migration": {"abort"},
		"Public IP Allocation": {
			"attach_to_virtual_machine", "detach_from_virtual_machine", "reserve", "release_reservation",
			"retry_operation", "block", "unblock",
		},
		"Public IP Pool": {"generate_available_allocations", "reserve_allocation", "retry_provider_operation"},
		"Proxy Server": {"provision", "update_dns_record", "update_package", "configure_telemetry"},
		"IPv6 Router Server": {"update_package", "configure_telemetry"},
		"Wireguard Gateway Server": {"update_package", "configure_telemetry"},
		"Cargo Server": {"enable_pilot_release_tracker", "disable_pilot_release_tracker"},
	},
	"dangerous": {
		"Metal Server": {"upgrade_metald", "toggle_cpu_template", "reboot_server", "drain"},
		"Virtual Machine": {"set_privileged", "set_network_gateway", "migrate"},
		"Virtual Machine Migration": {"restore_source"},
		"Virtual Machine Image": {"retry_transfer", "migrate_to_object_storage"},
		"Wireguard Gateway Server": {"rebuild"},
		"Metald Update": {"plan", "edit_plan", "start", "pause", "resume", "cancel"},
	},
}  # fmt: skip

EDITABLE_FIELD_TYPES = frozenset({"Check", "Data", "Float", "Int", "Link", "Select", "Small Text"})


def get_desk_actions(doctype: str) -> list[dict]:
	"""The Desk actions that an agent may run on a record of a DocType, with their parameters."""
	controller = get_controller(doctype)
	return [
		{
			"action": action,
			"needs_approval": group == "dangerous",
			"parameters": [
				str(parameter) for parameter in list(inspect.signature(function).parameters.values())[1:]
			],
			"summary": (inspect.getdoc(function) or "").split("\n", 1)[0],
		}
		for group, actions in ALLOWED_ACTIONS.items()
		for action in sorted(actions.get(doctype, ()))
		if (function := getattr(controller, action, None))
	]


def is_normal_action(doctype: str, name: str, action: str) -> bool:
	"""Return whether an action runs without approval: a normal Desk action, not on a customer VM."""
	if action not in ALLOWED_ACTIONS["normal"].get(doctype, ()):
		return False
	return doctype != "Virtual Machine" or frappe.db.get_value("Virtual Machine", name, "tenant_id") == 0


def run_desk_action(doctype: str, name: str, action: str, arguments: dict[str, Any] | None = None) -> Any:
	"""Run one allowed Desk action as the current user, with the same checks as a Desk button."""
	if not any(action in actions.get(doctype, ()) for actions in ALLOWED_ACTIONS.values()):
		frappe.throw(_("{0} has no Desk action {1} that an agent may run.").format(doctype, action))

	document = frappe.get_doc(doctype, name)
	document.check_permission("write")
	result = document.run_method(action, **(arguments or {}))
	return result.as_dict() if isinstance(result, Document) else result


def set_record_value(doctype: str, name: str, fieldname: str, value: Any) -> dict:
	"""Change one editable field of an Atlas record and save it, as an engineer does in the form."""
	if frappe.local.module_app.get(frappe.scrub(frappe.get_meta(doctype).module)) != "atlas":
		frappe.throw(_("{0} is not an Atlas DocType.").format(doctype))
	field = frappe.get_meta(doctype).get_field(fieldname)
	if not field or field.read_only or field.fieldtype not in EDITABLE_FIELD_TYPES:
		frappe.throw(_("An agent cannot change the field {0} of {1}.").format(fieldname, doctype))

	document = frappe.get_doc(doctype, name)
	document.check_permission("write")
	document.set(fieldname, value)
	document.save()
	return {"doctype": doctype, "name": name, fieldname: document.get(fieldname)}
