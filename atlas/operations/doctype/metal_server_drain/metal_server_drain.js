// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metal Server Drain", {
	setup(frm) {
		frm.set_query("virtual_machine", "virtual_machines", () => ({
			filters: {
				server: frm.doc.source_metal_server,
				is_draft: 0,
				is_terminating: 0,
			},
		}));
	},
});
