frappe.listview_settings["Virtual Machine Snapshot"] = {
	add_fields: ["status"],

	get_indicator(doc) {
		const config = {
			Pending: ["Pending", "orange", "status,=,Pending"],
			Available: ["Available", "green", "status,=,Available"],
			Failed: ["Failed", "red", "status,=,Failed"],
		}[doc.status];
		return config ? [__(config[0]), config[1], config[2]] : null;
	},
};
