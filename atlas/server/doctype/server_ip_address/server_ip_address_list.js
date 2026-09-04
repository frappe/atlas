function showReserveServerIPAddressDialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Reserve Public IPv4"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"This reserves one public IPv4 address from the provider. Attach it to a Virtual Machine later."
				)}</p>`,
			},
		],
		primary_action_label: __("Reserve"),
		primary_action() {
			frappe.call({
				method: "atlas.server.doctype.server_ip_address.server_ip_address.reserve",
				freeze: true,
				freeze_message: __("Reserving Public IPv4"),
				callback(response) {
					dialog.hide();
					frappe.set_route("Form", "Server IP Address", response.message);
				},
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["Server IP Address"] = {
	refresh(listview) {
		listview.page.clear_primary_action();
		if (!has_common(frappe.user_roles, ["System Manager"])) return;
		listview.page.add_inner_button(
			__("Reserve Public IPv4"),
			showReserveServerIPAddressDialog
		);
	},
};
