// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("WireGuard Gateway Server", {
	refresh(frm) {
		frm.disable_save();
		if (!has_common(frappe.user_roles, ["System Manager"]) || frm.doc.status === "Archived") {
			return;
		}

		frm.add_custom_button(
			__("Archive"),
			() =>
				frappe.confirm(
					__(
						"Archive {0}? Its gateway and API go offline, and its proxy route is removed.",
						[frm.doc.name]
					),
					() =>
						frm
							.call({ method: "archive", doc: frm.doc, freeze: true })
							.then(() => frm.reload_doc())
				),
			__("Dangerous Actions")
		);
	},
});
