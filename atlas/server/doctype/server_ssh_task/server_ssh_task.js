// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Server SSH Task", {
	refresh(frm) {
		frappe.realtime.off("server_ssh_task_output_update");
		frappe.realtime.on("server_ssh_task_output_update", (message) => {
			if (message.name == frm.doc.name) {
				frm.set_value("output", message.output);
			}
		});
	},
});
