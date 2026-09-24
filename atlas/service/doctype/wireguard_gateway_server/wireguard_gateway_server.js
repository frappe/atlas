// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("WireGuard Gateway Server", {
	refresh(frm) {
		frm.disable_save();
		if (!has_common(frappe.user_roles, ["System Manager"]) || frm.doc.status === "Archived") {
			return;
		}

		frm.add_custom_button(
			__("Sync Peers"),
			() =>
				frm
					.call({ method: "sync_peers", doc: frm.doc, freeze: true })
					.then(() => frm.reload_doc()),
			__("Actions")
		);

		frm.add_custom_button(
			__("Archive"),
			() =>
				frappe.confirm(
					__(
						"Archive {0}? Its WireGuard peers lose access to their tenant VMs.",
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
