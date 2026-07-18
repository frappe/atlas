from frappe import _


def get_data():
	return {
		"fieldname": "tenant",
		"transactions": [
			{"label": _("Compute"), "items": ["Virtual Machine"]},
			{"label": _("VPC access"), "items": ["VPN Peer"]},
		],
	}
