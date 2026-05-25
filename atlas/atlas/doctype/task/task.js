frappe.ui.form.on("Task", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.disable_save();
		}
	},
});


frappe.listview_settings["Task"] = {
	add_fields: ["status", "duration_milliseconds"],
	get_indicator(doc) {
		return {
			Pending: ["Pending", "orange", "status,=,Pending"],
			Running: ["Running", "blue", "status,=,Running"],
			Success: ["Success", "green", "status,=,Success"],
			Failure: ["Failure", "red", "status,=,Failure"],
		}[doc.status];
	},
};
