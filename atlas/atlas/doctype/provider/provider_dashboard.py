from frappe import _


def get_data():
	return {
		"fieldname": "provider",
		"transactions": [
			{"label": _("Servers"), "items": ["Server"]},
		],
	}
