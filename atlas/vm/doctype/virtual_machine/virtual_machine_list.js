function firewallRuleFields() {
	return [
		{
			fieldname: "direction",
			fieldtype: "Select",
			label: __("Direction"),
			options: "inbound\noutbound",
			reqd: 1,
			in_list_view: 1,
		},
		{
			fieldname: "protocol",
			fieldtype: "Select",
			label: __("Protocol"),
			options: "any\ntcp\nudp\nicmp",
			reqd: 1,
			in_list_view: 1,
		},
		{
			fieldname: "ports",
			fieldtype: "Data",
			label: __("Ports"),
			placeholder: "22 or 8000-9000",
			in_list_view: 1,
		},
		{
			fieldname: "cidrs",
			fieldtype: "Data",
			label: __("CIDRs"),
			reqd: 1,
			placeholder: "0.0.0.0/0, ::/0",
			in_list_view: 1,
		},
	];
}

function firewallValue(enabled, rows) {
	const firewall = { enabled: Boolean(enabled), inbound: [], outbound: [] };
	(rows || []).forEach((row) => {
		if (!row.direction && !row.protocol && !row.cidrs) return;
		if (!["inbound", "outbound"].includes(row.direction)) {
			frappe.throw(__("Each firewall rule needs a direction."));
		}
		firewall[row.direction].push({
			protocol: row.protocol,
			ports: (row.ports || "").trim(),
			cidrs: (row.cidrs || "")
				.split(/[\s,]+/)
				.map((cidr) => cidr.trim())
				.filter(Boolean),
		});
	});
	return firewall;
}

function publicAddressFields(getDialog, version) {
	const mode = `public_ipv${version}_mode`;
	const isReserved = `eval: doc.${mode} === "reserved"`;
	return [
		{
			fieldname: mode,
			fieldtype: "Select",
			label: __("IPv{0}", [version]),
			options: [
				{ label: __("None"), value: "none" },
				{ label: __("Automatic"), value: "auto" },
				{ label: __("Reserved allocation"), value: "reserved" },
			],
			default: "none",
		},
		{
			fieldname: `public_ipv${version}_allocation`,
			fieldtype: "Link",
			label: __("IPv{0} Allocation", [version]),
			options: "Public IP Allocation",
			depends_on: isReserved,
			mandatory_depends_on: isReserved,
			get_query: () => ({
				filters: {
					status: "Reserved",
					version,
					tenant_id: getDialog().get_value("tenant_id") || 0,
					virtual_machine: ["is", "not set"],
				},
			}),
		},
	];
}

function publicAddressSelector(values, version) {
	const mode = values[`public_ipv${version}_mode`];
	const allocation = values[`public_ipv${version}_allocation`];
	delete values[`public_ipv${version}_mode`];
	delete values[`public_ipv${version}_allocation`];
	if (mode === "auto") return "auto";
	if (mode === "reserved") return allocation;
	return "";
}

// A hostname label holds lowercase letters, digits, and inner hyphens, up to 63 characters.
function hostnameSlug(text) {
	return (text || "")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.slice(0, 63)
		.replace(/^-+|-+$/g, "");
}

function showCreateVirtualMachineDialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Create Virtual Machine"),
		size: "large",
		fields: [
			{ fieldtype: "Section Break", label: __("Machine") },
			{
				fieldname: "virtual_machine_image",
				fieldtype: "Link",
				label: __("Image"),
				options: "Virtual Machine Image",
				reqd: 1,
				filters: { enabled: 1, status: "Available" },
				async onchange() {
					const image = dialog.get_value("virtual_machine_image");
					if (!image) return;
					const { message } = await frappe.db.get_value(
						"Virtual Machine Image",
						image,
						"title"
					);
					dialog.set_value("hostname", hostnameSlug(message.title));
				},
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "hostname", fieldtype: "Data", label: __("Hostname") },
			{ fieldtype: "Column Break" },
			{ fieldname: "tenant_id", fieldtype: "Int", label: __("Tenant ID"), default: 0 },

			{ fieldtype: "Section Break", label: __("Resources") },
			{
				fieldname: "cpu_millicores",
				fieldtype: "Int",
				label: __("CPU (millicores)"),
				reqd: 1,
				min: 100,
				max: 32000,
				default: 1000,
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "memory_mib",
				fieldtype: "Int",
				label: __("Memory (MiB)"),
				reqd: 1,
				default: 1024,
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "disk_mib",
				fieldtype: "Int",
				label: __("Disk (MiB)"),
				reqd: 1,
				default: 10240,
			},

			{ fieldtype: "Section Break", label: __("Limits (0 = no limit)") },
			{
				fieldname: "disk_throughput_mibps",
				fieldtype: "Int",
				label: __("Disk Throughput (MiB/s)"),
				default: 0,
			},
			{
				fieldname: "private_network_throughput_mibps",
				fieldtype: "Int",
				label: __("Private Network (MiB/s)"),
				default: 0,
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "disk_iops", fieldtype: "Int", label: __("Disk IOPS"), default: 0 },
			{
				fieldname: "public_network_throughput_mibps",
				fieldtype: "Int",
				label: __("Public Network (MiB/s)"),
				default: 0,
			},

			{ fieldtype: "Section Break", label: __("Public IP") },
			...publicAddressFields(() => dialog, "4"),
			{ fieldtype: "Column Break" },
			...publicAddressFields(() => dialog, "6"),

			{ fieldtype: "Section Break", label: __("Firewall") },
			{
				fieldname: "firewall_enabled",
				fieldtype: "Check",
				label: __("Enabled"),
				default: 0,
			},
			{
				fieldname: "firewall_rules",
				fieldtype: "Table",
				label: __("Allow Rules"),
				depends_on: "eval: doc.firewall_enabled",
				in_place_edit: true,
				data: [],
				fields: firewallRuleFields(),
			},

			{ fieldtype: "Section Break", label: __("Guest") },
			{
				fieldname: "ssh_keys",
				fieldtype: "Code",
				label: __("SSH Keys (one per line)"),
				min_lines: 8,
				max_lines: 8,
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "user_data",
				fieldtype: "Code",
				label: __("User Data (YAML)"),
				options: "YAML",
				min_lines: 8,
				max_lines: 8,
			},
		],
		primary_action_label: __("Create"),
		primary_action(values) {
			values.public_ipv4 = publicAddressSelector(values, "4");
			values.public_ipv6 = publicAddressSelector(values, "6");
			values.firewall = firewallValue(values.firewall_enabled, values.firewall_rules);
			delete values.firewall_enabled;
			delete values.firewall_rules;
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
		listview.page.set_primary_action(
			__("Create Virtual Machine"),
			showCreateVirtualMachineDialog
		);
	},
};
