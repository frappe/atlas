from frappe import _


def get_data():
	return {
		"fieldname": "virtual_machine",
		"transactions": [
			{"label": _("Operations"), "items": ["Task"]},
			{"label": _("Disk"), "items": ["Virtual Machine Snapshot"]},
			{"label": _("Network access"), "items": ["VPN Tunnel", "Firewall"]},
		],
	}
