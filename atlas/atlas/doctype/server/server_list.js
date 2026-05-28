frappe.listview_settings["Server"] = {
	add_fields: ["status"],

	get_indicator(doc) {
		const config = {
			Pending: ["Pending", "orange", "status,=,Pending"],
			Bootstrapping: ["Bootstrapping", "yellow", "status,=,Bootstrapping"],
			Active: ["Active", "green", "status,=,Active"],
			Draining: ["Draining", "yellow", "status,=,Draining"],
			Broken: ["Broken", "red", "status,=,Broken"],
			Archived: ["Archived", "grey", "status,=,Archived"],
		}[doc.status];
		return config ? [__(config[0]), config[1], config[2]] : null;
	},
};
