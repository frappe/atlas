// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function showCreateIPv6RouterServerDialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Create IPv6 Router Server"),
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
				fieldname: "public_ip_pool",
				fieldtype: "Link",
				label: __("Public IPv6 Pool"),
				options: "Public IP Pool",
				reqd: 1,
				description: __("A /84 or larger block. The router maps it to WG Mesh addresses."),
				filters: { version: "6", gateway: ["is", "not set"] },
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			frappe.call({
				method: "atlas.service.doctype.ipv6_router_server.ipv6_router_server.create",
				args: { request: values },
				freeze: true,
				freeze_message: __("Creating IPv6 Router Server..."),
				callback(response) {
					dialog.hide();
					frappe.set_route("Form", "IPv6 Router Server", response.message.name);
				},
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["IPv6 Router Server"] = {
	refresh(listview) {
		listview.page.clear_primary_action();
		if (!has_common(frappe.user_roles, ["System Manager"])) return;
		listview.page.add_inner_button(
			__("Create IPv6 Router Server"),
			showCreateIPv6RouterServerDialog
		);
	},
};
