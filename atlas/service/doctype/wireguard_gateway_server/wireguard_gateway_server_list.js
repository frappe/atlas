// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function showCreateWireguardGatewayServerDialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Create Wireguard Gateway Server"),
		fields: [
			{
				fieldname: "virtual_machine_image",
				fieldtype: "Link",
				label: __("Virtual Machine Image"),
				options: "Virtual Machine Image",
				reqd: 1,
				filters: { enabled: 1, status: "Available", image_type: "system" },
			},
			{
				fieldname: "cpu_millicores",
				fieldtype: "Int",
				label: __("CPU (millicores)"),
				description: __("1000 millicores equals one CPU core."),
				reqd: 1,
				default: 2000,
			},
			{
				fieldname: "memory_mib",
				fieldtype: "Int",
				label: __("Memory (MiB)"),
				reqd: 1,
				default: 2048,
			},
			{
				fieldname: "disk_mib",
				fieldtype: "Int",
				label: __("Disk (MiB)"),
				reqd: 1,
				default: 8192,
			},
			{
				fieldname: "public_ipv4",
				fieldtype: "Link",
				label: __("Public IPv4 Allocation"),
				options: "Public IP Allocation",
				reqd: 1,
				description: __("The WireGuard endpoint and the gateway API use this address."),
				filters: {
					status: "Reserved",
					version: "4",
					tenant_id: 0,
					virtual_machine: ["is", "not set"],
				},
			},
			{
				fieldname: "listen_port",
				fieldtype: "Int",
				label: __("Listen Port"),
				description: __("UDP port of the WireGuard endpoint."),
				reqd: 1,
				default: 51820,
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			frappe.call({
				method: "atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server.create",
				args: { request: values },
				freeze: true,
				freeze_message: __("Creating Wireguard Gateway Server..."),
				callback(response) {
					dialog.hide();
					frappe.set_route("Form", "Wireguard Gateway Server", response.message.name);
				},
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["Wireguard Gateway Server"] = {
	refresh(listview) {
		listview.page.clear_primary_action();
		if (!has_common(frappe.user_roles, ["System Manager"])) return;
		listview.page.add_inner_button(
			__("Create Wireguard Gateway Server"),
			showCreateWireguardGatewayServerDialog
		);
	},
};
