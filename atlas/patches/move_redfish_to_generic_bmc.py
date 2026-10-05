import frappe


def execute() -> None:
	"""Select the Generic driver for existing provider settings."""
	provider = frappe.db.get_single_value("Atlas Settings", "server_provider")
	if provider == "Redfish":
		frappe.db.set_single_value(
			"Atlas Settings", {"server_provider": "Generic", "generic_provider_driver": "BMC"}
		)
	elif provider == "Generic" and not frappe.db.get_single_value(
		"Atlas Settings", "generic_provider_driver"
	):
		frappe.db.set_single_value("Atlas Settings", "generic_provider_driver", "SSH")
