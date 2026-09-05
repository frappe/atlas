function showCreateVirtualMachineDialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Create Virtual Machine"),
		size: "large",
		fields: [
			{ fieldtype: "Section Break", label: __("Machine") },
			{
				fieldname: "virtual_machine_image",
				fieldtype: "Link",
				label: __("Virtual Machine Image"),
				options: "Virtual Machine Image",
				reqd: 1,
				filters: { enabled: 1, status: "Available" },
			},
			{ fieldname: "vcpus", fieldtype: "Int", label: __("vCPUs"), reqd: 1, default: 1 },
			{
				fieldname: "disk_throughput_mibps",
				fieldtype: "Int",
				label: __("Disk Throughput (MiB/s)"),
				default: 0,
				description: __("0 does not apply a limit."),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "memory_mib",
				fieldtype: "Int",
				label: __("Memory (MiB)"),
				reqd: 1,
				default: 1024,
			},
			{
				fieldname: "disk_mib",
				fieldtype: "Int",
				label: __("Disk (MiB)"),
				reqd: 1,
				default: 10240,
			},
			{
				fieldname: "disk_iops",
				fieldtype: "Int",
				label: __("Disk IOPS"),
				default: 0,
				description: __("0 does not apply a limit."),
			},
			{ fieldtype: "Section Break", label: __("Guest") },
			{ fieldname: "hostname", fieldtype: "Data", label: __("Hostname") },
			{
				fieldname: "ssh_keys",
				fieldtype: "Code",
				label: __("SSH Keys"),
				description: __("One public key per line."),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "user_data",
				fieldtype: "Code",
				label: __("User Data"),
				options: "YAML",
			},
			{ fieldtype: "Section Break", label: __("Network") },
			{
				fieldname: "egress",
				fieldtype: "Select",
				label: __("Egress"),
				options: "uplink\nmesh\nnone",
				default: "uplink",
				reqd: 1,
				description: __("uplink reaches the internet. mesh reaches tenant VMs only."),
			},
			{
				fieldname: "tenant_id",
				fieldtype: "Int",
				label: __("Tenant ID"),
				description: __("VMs in the same tenant can connect through the mesh."),
				reqd: 1,
			},
			{
				fieldname: "is_privileged",
				fieldtype: "Check",
				label: __("Privileged"),
				default: 0,
				description: __("Reaches every tenant. Needs tenant 0."),
			},
			{
				fieldname: "server_ip_address",
				fieldtype: "Link",
				label: __("Public IPv4"),
				options: "Server IP Address",
				depends_on: 'eval:doc.egress == "uplink"',
				filters: { status: "Allocated" },
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "private_network_throughput_mibps",
				fieldtype: "Int",
				label: __("Private Network Throughput (MiB/s)"),
				default: 0,
				description: __("0 does not apply a limit."),
			},
			{
				fieldname: "public_network_throughput_mibps",
				fieldtype: "Int",
				label: __("Public Network Throughput (MiB/s)"),
				default: 0,
				description: __("0 does not apply a limit."),
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			frappe.call({
				method: "atlas.vm.doctype.virtual_machine.virtual_machine.create",
				args: { request: values },
				freeze: true,
				freeze_message: __("Sending Virtual Machine request"),
				callback(response) {
					dialog.hide();
					if (response.message.is_draft) {
						frappe.show_alert({
							message: __(
								"Metal did not confirm the request. Atlas kept the draft."
							),
							indicator: "orange",
						});
					}
					frappe.set_route("Form", "Virtual Machine", response.message.name);
				},
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["Virtual Machine"] = {
	refresh(listview) {
		listview.page.clear_primary_action();
		if (!has_common(frappe.user_roles, ["System Manager"])) return;
		listview.page.add_inner_button(
			__("Create Virtual Machine"),
			showCreateVirtualMachineDialog
		);
	},
};
