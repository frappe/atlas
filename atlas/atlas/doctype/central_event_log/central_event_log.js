// Copyright (c) 2026, Frappe and Contributors
// For license information, please see license.txt

frappe.ui.form.on("Central Event Log", {
	refresh(frm) {
		if (["queued", "error", "skipped"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry Delivery"), () => {
				frm.call("retry_delivery").then(() => {
					frappe.show_alert({ message: __("Delivery re-attempted"), indicator: "blue" });
					frm.reload_doc();
				});
			});
		}
	},
});
