function showReserveServerIPAddressDialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Reserve Public IPv4"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"This adds one provider address to the shared pool for tenant claims."
				)}</p>`,
			},
		],
		primary_action_label: __("Reserve"),
		primary_action() {
			frappe.call({
				method: "atlas.metal_server.doctype.metal_server_ip_address.metal_server_ip_address.reserve_for_pool",
				freeze: true,
				freeze_message: __("Reserving Public IPv4"),
				callback(response) {
					dialog.hide();
					frappe.set_route("Form", "Metal Server IP Address", response.message);
				},
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["Metal Server IP Address"] = {
	async onload(listview) {
		listview.server_provider = await frappe.db.get_single_value(
			"Atlas Settings",
			"server_provider"
		);
		listview.refresh();
	},
	refresh(listview) {
		// Generic addresses are added by hand with the standard Add button.
		if (!listview.server_provider || listview.server_provider === "Generic") return;

		listview.page.clear_primary_action();
		if (!has_common(frappe.user_roles, ["System Manager"])) return;
		listview.page.add_inner_button(
			__("Reserve Public IPv4"),
			showReserveServerIPAddressDialog
		);
	},
};
