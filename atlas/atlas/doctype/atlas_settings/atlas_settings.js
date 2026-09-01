// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Atlas Settings", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		[
			[
				__("Setup server provider"),
				"setup_server_provider",
				!frm.doc.is_setup_completed && !frm.doc.is_server_provider_setup_completed,
				true,
			],
			[
				__("Setup DNS"),
				"setup_dns_provider",
				!frm.doc.is_setup_completed && !frm.doc.is_dns_setup_completed,
				true,
			],
			[__("Sync server sizes"), "sync_server_sizes", frm.doc.is_setup_completed, true],
			[__("Sync server images"), "sync_server_images", frm.doc.is_setup_completed, true],
		].forEach(([label, method, condition, grouped]) => {
			if (condition) {
				frm.add_custom_button(
					label,
					() => {
						frappe.confirm(`Are you sure you want to ${label.toLowerCase()}?`, () =>
							frm
								.call(method, {
									freeze: true,
									freeze_message: __("Please wait..."),
								})
								.then(() => frm.refresh()),
						);
					},
					grouped ? __("Actions") : null,
				);
			}
		});
	},
});
