frappe.ui.form.on("Virtual Machine", {
	refresh(frm) {
		if (frm.is_new()) {
			frappe.set_route("List", "Virtual Machine");
			return;
		}

		// All fields mirror Metal and are edited through actions, never a direct save.
		frm.disable_save();
		loadFirewallConfiguration(frm);

		const current_state = frm.doc.current_state;
		const is_running = current_state === "running";
		const is_stopped = current_state === "stopped";
		const is_paused = current_state === "paused";
		const is_live = is_running || is_stopped || is_paused;
		const is_migrating = Boolean(frm.doc.active_migration);

		const CONSOLE = __("Console Access");
		const ACTIONS = __("Actions");
		const DANGEROUS = __("Dangerous Actions");

		// A string action calls that method on the document. A function opens a dialog.
		[
			[
				__("TTY Console"),
				() => openConsole(frm, "tty"),
				is_running || is_paused || current_state === "created",
				CONSOLE,
			],
			[__("SSH Console"), () => openConsole(frm, "ssh"), is_running, CONSOLE],
			[__("Start VM"), "start", is_stopped, ACTIONS, __("Starting...")],
			[__("Stop VM"), "stop", is_running || is_paused, ACTIONS, __("Stopping...")],
			[__("Reboot VM"), "reboot", is_running, ACTIONS, __("Rebooting...")],
			[__("Pause VM"), "pause", is_running, ACTIONS, __("Pausing...")],
			[__("Resume VM"), "resume", is_paused, ACTIONS, __("Resuming...")],
			[__("Snapshot VM"), () => showCreateMachineImageDialog(frm), true, ACTIONS],
			[__("Resize Disk"), () => showResizeDiskDialog(frm), true, ACTIONS],
			[__("Edit Disk Limits"), () => showEditDiskLimitsDialog(frm), true, ACTIONS],
			[__("Resize VM"), () => showResizeDialog(frm), true, ACTIONS],
			[__("Edit SSH Keys"), () => showEditSSHKeysDialog(frm), true, ACTIONS],
			[__("Edit Metadata"), () => showEditMetadataDialog(frm), true, ACTIONS],
			[__("Edit Network Throughput"), () => showEditThroughputDialog(frm), true, ACTIONS],
			[__("Edit Firewall"), () => showEditFirewallDialog(frm), true, ACTIONS],
			[
				__("Attach IP Address"),
				() => showAttachIPAddressDialog(frm),
				!frm.doc.public_ipv4,
				ACTIONS,
			],
			[
				__("Detach IP Address"),
				() => detachIPAddress(frm),
				Boolean(frm.doc.public_ipv4),
				ACTIONS,
			],
			[__("Change Egress Mode"), () => showEgressDialog(frm), true, ACTIONS],
			[
				frm.doc.is_termination_protected
					? __("Disable Termination Protection")
					: __("Enable Termination Protection"),
				() => setTerminationProtection(frm),
				true,
				ACTIONS,
			],
			[
				__("View Migration"),
				() =>
					frappe.set_route(
						"Form",
						"Virtual Machine Migration",
						frm.doc.active_migration
					),
				is_migrating,
				ACTIONS,
			],
			// Only a tenant 0 VM can hold the Atlas WG Mesh privilege.
			[
				frm.doc.is_privileged ? __("Revoke Privilege") : __("Grant Privilege"),
				() => showPrivilegeDialog(frm),
				frm.doc.tenant_id === 0,
				DANGEROUS,
			],
			[
				__("Migrate VM"),
				() => migrateVirtualMachine(frm),
				is_live && !is_migrating,
				DANGEROUS,
			],
			[__("Terminate VM"), () => terminateVirtualMachine(frm), true, DANGEROUS],
		].forEach(([label, action, condition, group, freeze_message]) => {
			if (!condition) {
				return;
			}

			const call = () =>
				frm
					.call({ method: action, doc: frm.doc, freeze: true, freeze_message })
					.then(() => frm.reload_doc());

			frm.add_custom_button(label, typeof action === "string" ? call : action, group);
		});
	},
});

function setTerminationProtection(frm) {
	const protecting = !frm.doc.is_termination_protected;
	const message = protecting
		? __("Protect {0}? Termination and deletion are refused until you disable this.", [
				frm.doc.name.bold(),
		  ])
		: __("Unprotect {0}? Anyone with write access can then terminate it.", [
				frm.doc.name.bold(),
		  ]);

	frappe.confirm(message, () =>
		frm
			.call({
				method: "set_termination_protection",
				doc: frm.doc,
				args: { is_protected: protecting },
				freeze: true,
				freeze_message: __("Saving..."),
			})
			.then(() => frm.reload_doc())
	);
}

function terminateVirtualMachine(frm) {
	frappe.confirm(
		__("Terminate {0}? The virtual machine and its data are lost.", [frm.doc.name.bold()]),
		() =>
			frm
				.call({
					method: "terminate",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Requesting termination..."),
				})
				.then(() => frm.reload_doc())
	);
}

function migrateVirtualMachine(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Migrate Virtual Machine"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "summary",
				options: __(
					"This moves {0} off {1}. It stays available until the short cutover.<br><br>",
					[frm.doc.name.bold(), (frm.doc.server || "").bold()]
				),
			},
			{
				fieldtype: "Link",
				fieldname: "destination_metal_server",
				label: __("Destination Metal Server"),
				options: "Metal Server",
				description: __("Leave empty to let auto select a Metal Server."),
				get_query: () => ({
					filters: {
						status: "Running",
						is_provisioning_completed: 1,
						name: ["!=", frm.doc.server],
					},
				}),
			},
		],
		primary_action_label: __("Migrate"),
		primary_action(values) {
			dialog.hide();
			frm.call({
				method: "migrate",
				doc: frm.doc,
				args: {
					destination_metal_server: values.destination_metal_server || null,
				},
				freeze: true,
				freeze_message: __("Scheduling migration..."),
			}).then((response) => {
				if (response.message) {
					frappe.set_route("Form", "Virtual Machine Migration", response.message);
				} else {
					frm.reload_doc();
				}
			});
		},
	});
	dialog.show();
}

function showPrivilegeDialog(frm) {
	const granting = !frm.doc.is_privileged;
	const lines = granting
		? [
				__("This Virtual Machine reaches every tenant, and every tenant reaches it."),
				__("A privileged Virtual Machine must use tenant 0."),
		  ]
		: [__("This Virtual Machine stops reaching other tenants, and they stop reaching it.")];
	lines.push(__("Each host applies the change on its next sync. It can take up to 30 seconds."));

	frappe.confirm(lines.map((line) => `<p>${line}</p>`).join(""), () =>
		frm
			.call({
				method: "set_privileged",
				doc: frm.doc,
				args: { is_privileged: granting },
				freeze: true,
				freeze_message: granting
					? __("Granting privilege...")
					: __("Revoking privilege..."),
			})
			.then(() => {
				frappe.show_alert({
					message: __("Every host applies this within 30 seconds."),
					indicator: "orange",
				});
				frm.reload_doc();
			})
	);
}

function openConsole(frm, mode) {
	// Open a tab before the async token request to avoid popup blocking.
	const consoleTab = window.open("about:blank", "_blank");
	frm.call({
		method: "get_console_token",
		doc: frm.doc,
		args: { mode },
		freeze: true,
		freeze_message: __("Opening console..."),
	}).then((response) => {
		const token = response.message && response.message.token;
		if (!token) {
			if (consoleTab) consoleTab.close();
			frappe.msgprint(__("Could not open the console."));
			return;
		}
		// Keep the token in the fragment.
		const url = "/vm_console#token=" + encodeURIComponent(token);
		if (consoleTab) {
			consoleTab.location = url;
		} else {
			window.open(url, "_blank");
		}
	});
}

function showCreateMachineImageDialog(frm) {
	frappe.prompt(
		[
			{ fieldname: "title", fieldtype: "Data", label: __("Image Title"), reqd: 1 },
			{ fieldname: "cache_image", fieldtype: "Check", label: __("Cache Image") },
			{
				fieldname: "memory_snapshot",
				fieldtype: "Check",
				label: __("Memory Snapshot"),
				description: __("Uses this VM's current CPU, memory, and disk."),
			},
			{
				fieldname: "is_termination_protected",
				fieldtype: "Check",
				label: __("Termination Protection"),
				description: __("The image cannot be deleted until this is disabled."),
			},
		],
		(values) =>
			frm
				.call({
					method: "create_machine_image",
					doc: frm.doc,
					args: values,
					freeze: true,
					freeze_message: __("Creating image record..."),
				})
				.then((response) =>
					frappe.set_route("Form", "Virtual Machine Image", response.message)
				),
		__("Create Machine Image"),
		__("Create")
	);
}

function showResizeDiskDialog(frm) {
	frappe.prompt(
		{
			fieldname: "disk_mib",
			fieldtype: "Int",
			label: __("New Disk Size (MiB)"),
			reqd: 1,
			default: frm.doc.disk_mib,
			description: __("The disk can only grow. Current size is {0} MiB.", [
				frm.doc.disk_mib,
			]),
		},
		({ disk_mib }) =>
			frm
				.call({
					method: "resize_disk",
					doc: frm.doc,
					args: { disk_mib },
					freeze: true,
					freeze_message: __("Resizing disk..."),
				})
				.then(() => frm.reload_doc()),
		__("Resize Disk"),
		__("Resize")
	);
}

function showEditSSHKeysDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Edit SSH Keys"),
		size: "large",
		fields: [
			{
				fieldname: "ssh_keys",
				fieldtype: "Small Text",
				label: __("SSH Keys"),
				default: frm.doc.ssh_keys,
				description: __("One public key per line."),
			},
		],
		primary_action_label: __("Save"),
		primary_action({ ssh_keys }) {
			dialog.hide();
			frm.call({
				method: "replace_ssh_keys",
				doc: frm.doc,
				args: {
					ssh_keys: (ssh_keys || "")
						.split("\n")
						.map((line) => line.trim())
						.filter((line) => line),
				},
				freeze: true,
				freeze_message: __("Updating SSH keys..."),
			}).then(() => frm.reload_doc());
		},
	});
	dialog.show();
}

function showEditMetadataDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Edit Metadata"),
		fields: [
			{
				fieldname: "metadata",
				fieldtype: "Code",
				options: "JSON",
				label: __("Metadata"),
				default: frm.doc.metadata || "{}",
				description: __("A JSON object of string keys and values."),
			},
		],
		primary_action_label: __("Save"),
		primary_action({ metadata }) {
			let parsed;
			try {
				parsed = JSON.parse(metadata || "{}");
			} catch (error) {
				frappe.msgprint(__("Metadata must be valid JSON."));
				return;
			}
			dialog.hide();
			frm.call({
				method: "replace_metadata",
				doc: frm.doc,
				args: { metadata: parsed },
				freeze: true,
				freeze_message: __("Updating metadata..."),
			}).then(() => frm.reload_doc());
		},
	});
	dialog.show();
}

function showResizeDialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "cpu_millicores",
				fieldtype: "Int",
				label: __("CPU (millicores)"),
				reqd: 1,
				min: 100,
				max: 32000,
				default: frm.doc.cpu_millicores,
				description: __(
					"The valid range is 100 to 32000 millicores. 1000 millicores equals one CPU core."
				),
				show_description_on_click: 1,
			},
			{
				fieldname: "memory_mib",
				fieldtype: "Int",
				label: __("Memory (MiB)"),
				reqd: 1,
				default: frm.doc.memory_mib,
			},
			{
				fieldname: "disk_mib",
				fieldtype: "Int",
				label: __("Disk (MiB)"),
				reqd: 1,
				default: frm.doc.disk_mib,
				description: __("The disk can only grow."),
			},
			{
				fieldname: "sleep_after_idle_seconds",
				fieldtype: "Int",
				label: __("Sleep After Idle (Seconds)"),
				default: frm.doc.sleep_after_idle_seconds,
				description: __("0 disables automatic idle sleep."),
			},
		],
		(values) =>
			frm
				.call({
					method: "resize",
					doc: frm.doc,
					args: values,
					freeze: true,
					freeze_message: __("Resizing..."),
				})
				.then((response) => {
					if (response.message) {
						frappe.show_alert({
							message: __("The host has no room. Moving the VM to another host."),
							indicator: "blue",
						});
					}
					frm.reload_doc();
				}),
		__("Resize VM"),
		__("Resize")
	);
}

function showEditThroughputDialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "private_network_throughput_mibps",
				fieldtype: "Int",
				label: __("Private Throughput (MiB/s)"),
				default: frm.doc.private_network_throughput_mibps,
				description: __("0 does not apply a limit."),
			},
			{
				fieldname: "public_network_throughput_mibps",
				fieldtype: "Int",
				label: __("Public Throughput (MiB/s)"),
				default: frm.doc.public_network_throughput_mibps,
				description: __("0 does not apply a limit."),
			},
		],
		(values) =>
			frm
				.call({
					method: "update_network_throughput",
					doc: frm.doc,
					args: values,
					freeze: true,
					freeze_message: __("Updating network throughput..."),
				})
				.then(() => frm.reload_doc()),
		__("Edit Network Throughput"),
		__("Save")
	);
}

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
			description: __("Use one port or one range, such as 22 or 8000-9000."),
			in_list_view: 1,
		},
		{
			fieldname: "cidrs",
			fieldtype: "Data",
			label: __("CIDRs"),
			reqd: 1,
			description: __("Separate IPv4 and IPv6 prefixes with commas or spaces."),
			in_list_view: 1,
		},
	];
}

function firewallRows(value) {
	const rows = [];
	["inbound", "outbound"].forEach((direction) => {
		(value[direction] || []).forEach((rule) => {
			rows.push({
				direction,
				protocol: rule.protocol,
				ports: rule.ports || "",
				cidrs: (rule.cidrs || []).join(", "),
			});
		});
	});
	return rows;
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

function showEditFirewallDialog(frm) {
	if (frm.firewall_configuration) {
		openEditFirewallDialog(frm, frm.firewall_configuration);
		return;
	}

	loadFirewallConfiguration(frm, true).then((firewall) => openEditFirewallDialog(frm, firewall));
}

function loadFirewallConfiguration(frm, freeze = false) {
	return frm
		.call({
			method: "read_firewall",
			doc: frm.doc,
			type: "GET",
			freeze,
			freeze_message: __("Reading firewall..."),
		})
		.then(({ message }) => {
			frm.firewall_configuration = message;
			frm.doc.firewall_summary = JSON.stringify(message, null, 2);
			frm.refresh_field("firewall_summary");
			return message;
		});
}

function openEditFirewallDialog(frm, current) {
	const dialog = new frappe.ui.Dialog({
		title: __("Edit Firewall"),
		size: "large",
		fields: [
			{
				fieldname: "enabled",
				fieldtype: "Check",
				label: __("Enabled"),
				default: current.enabled,
			},
			{
				fieldname: "rules",
				fieldtype: "Table",
				label: __("Allow Rules"),
				in_place_edit: true,
				data: firewallRows(current),
				fields: firewallRuleFields(),
			},
		],
		primary_action_label: __("Save"),
		primary_action({ enabled, rules }) {
			const firewall = firewallValue(enabled, rules);
			dialog.hide();
			frm.call({
				method: "update_firewall",
				doc: frm.doc,
				args: { firewall },
				freeze: true,
				freeze_message: __("Updating firewall..."),
			}).then(() => frm.reload_doc());
		},
	});
	dialog.show();
}

function showEgressDialog(frm) {
	frappe.prompt(
		{
			fieldname: "egress",
			fieldtype: "Select",
			label: __("Egress"),
			options: ["uplink", "mesh", "none"],
			reqd: 1,
			default: frm.doc.egress,
			description: __(
				"uplink reaches the internet. mesh reaches tenant VMs only. none isolates the VM. Active connections can stop."
			),
		},
		({ egress }) =>
			frm
				.call({
					method: "update_egress",
					doc: frm.doc,
					args: { egress },
					freeze: true,
					freeze_message: __("Changing egress mode..."),
				})
				.then(() => frm.reload_doc()),
		__("Change Egress Mode"),
		__("Save")
	);
}

function showAttachIPAddressDialog(frm) {
	frappe.prompt(
		{
			fieldname: "server_ip_address",
			fieldtype: "Link",
			label: __("Metal Server IP Address"),
			options: "Metal Server IP Address",
			reqd: 1,
			filters: { status: "Allocated" },
		},
		({ server_ip_address }) =>
			frm
				.call({
					method: "attach_ip_address",
					doc: frm.doc,
					args: { server_ip_address },
					freeze: true,
					freeze_message: __("Attaching IP address..."),
				})
				.then(() => frm.reload_doc()),
		__("Attach IP Address"),
		__("Attach")
	);
}

function detachIPAddress(frm) {
	frappe.confirm(
		__("Detach {0} from this Virtual Machine? Active connections can stop.", [
			frm.doc.public_ipv4,
		]),
		() =>
			frm
				.call({
					method: "detach_ip_address",
					doc: frm.doc,
					freeze: true,
					freeze_message: __("Detaching IP address..."),
				})
				.then(() => frm.reload_doc())
	);
}

function showEditDiskLimitsDialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "disk_throughput_mibps",
				fieldtype: "Int",
				label: __("Disk Throughput (MiB/s)"),
				default: frm.doc.disk_throughput_mibps,
				description: __("Covers reads and writes. 0 does not apply a limit."),
			},
			{
				fieldname: "disk_iops",
				fieldtype: "Int",
				label: __("Disk IOPS"),
				default: frm.doc.disk_iops,
				description: __("Covers reads and writes. 0 does not apply a limit."),
			},
		],
		(values) =>
			frm
				.call({
					method: "update_disk_limits",
					doc: frm.doc,
					args: values,
					freeze: true,
					freeze_message: __("Updating disk limits..."),
				})
				.then(() => frm.reload_doc()),
		__("Edit Disk Limits"),
		__("Save")
	);
}
