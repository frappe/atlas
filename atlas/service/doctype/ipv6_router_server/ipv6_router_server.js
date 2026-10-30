// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("IPv6 Router Server", {
	refresh(frm) {
		frm.disable_save();
		if (!has_common(frappe.user_roles, ["System Manager"]) || frm.doc.status === "Archived") {
			return;
		}

		if (frm.doc.status === "Active") {
			frm.add_custom_button(
				__("Update Package"),
				() =>
					frappe.confirm(
						__(
							"Install the published package on {0}? Routed IPv6 traffic stops for a moment while the router reloads.",
							[frm.doc.name]
						),
						() => frm.call({ method: "update_package", doc: frm.doc, freeze: true })
					),
				__("Actions")
			);
			frm.add_custom_button(
				__("Configure Telemetry"),
				() =>
					frappe.confirm(
						__("Install Vector and a new Datum token on {0}?", [frm.doc.name]),
						() =>
							frm.call({ method: "configure_telemetry", doc: frm.doc, freeze: true })
					),
				__("Actions")
			);
		}

		frm.add_custom_button(
			__("Archive"),
			() =>
				frappe.confirm(
					__(
						"Archive {0}? VMs with a routed IPv6 address from this router lose their public IPv6 path.",
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
