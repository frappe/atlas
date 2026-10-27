// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Proxy Server", {
	refresh(frm) {
		frm.disable_save();
		if (frm.is_new()) {
			return;
		}

		[
			[__("Re-provision"), "provision", frm.doc.status !== "Archived", "Actions"],
			[
				__("Update DNS record"),
				"update_dns_record",
				frm.doc.status !== "Archived" && frm.doc.virtual_machine,
				"Actions",
			],
			[
				__("Update Package"),
				"update_package",
				frm.doc.status === "Active",
				"Actions",
				__(
					"Install the published package on {0}? Open connections drop while OpenResty restarts.",
					[frm.doc.name]
				),
			],
			[
				__("Configure Telemetry"),
				"configure_telemetry",
				frm.doc.status === "Active",
				"Actions",
			],
			[__("Archive"), "archive", frm.doc.status !== "Archived", "Dangerous Actions"],
		].forEach(([label, method, condition, group, message]) => {
			if (!condition) {
				return;
			}

			frm.add_custom_button(
				label,
				() => {
					frappe.confirm(
						message || `Are you sure you want to ${label.toLowerCase()}?`,
						() =>
							frm
								.call(method, {
									freeze: true,
									freeze_message: __("Please wait..."),
								})
								.then(() => frm.refresh())
					);
				},
				__(group)
			);
		});

		frappe.db.get_single_value("Atlas Settings", "wildcard_domain").then((wildcard_domain) => {
			if (wildcard_domain) {
				frm.add_web_link(
					`https://${frm.doc.name}.${wildcard_domain}/docs`,
					__("Open proxy server docs")
				);
			}
		});
	},
});
