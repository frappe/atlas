// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Atlas Settings", {
	refresh(frm) {
		frm.call("get_form_autocomplete_options").then(({ message }) => {
			frm.fields_dict.placement_strategy.set_data(message.placement_strategies);
			frm.fields_dict.default_metal_machine_size.set_data(
				message.available_metal_machine_sizes
			);
			frm.fields_dict.default_metal_machine_image.set_data(
				message.available_metal_machine_images
			);
		});

		if (frm.is_new()) {
			return;
		}

		[
			[
				__("Setup server provider"),
				"setup_server_provider",
				!frm.doc.is_setup_completed && !frm.doc.is_server_provider_setup_completed,
				true,
				"Setup",
			],
			[
				__("Setup DNS"),
				"setup_dns_provider",
				!frm.doc.is_setup_completed && !frm.doc.is_dns_setup_completed,
				true,
				"Setup",
			],
			[
				__("Sync server sizes"),
				"sync_server_sizes",
				frm.doc.is_setup_completed,
				true,
				"Actions",
			],
			[
				__("Sync server images"),
				"sync_server_images",
				frm.doc.is_setup_completed,
				true,
				"Actions",
			],
			[
				__("Renew TLS certificate"),
				"renew_wildcard_certificate",
				frm.doc.is_dns_setup_completed,
				true,
				"Proxy",
			],
			[
				__("View proxy cluster password"),
				"view_proxy_cluster_password",
				frm.doc.is_setup_completed,
				true,
				"Proxy",
			],
			[
				__("Rotate proxy cluster password"),
				"rotate_proxy_cluster_password",
				true,
				true,
				"Proxy",
			],
		].forEach(([label, method, condition, grouped, group_name]) => {
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
								.then(() => frm.refresh())
						);
					},
					grouped ? __(group_name) : null
				);
			}
		});
	},
});
