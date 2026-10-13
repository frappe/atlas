frappe.ui.form.on("Virtual Machine", {
	refresh(frm) {
		if (frm.is_new()) {
			frappe.set_route("List", "Virtual Machine");
			return;
		}

		// All fields mirror Metal and are edited through actions, never a direct save.
		frm.disable_save();

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
			[
				__("Rescue Mode"),
				() => showRescueDialog(frm),
				(is_live || current_state === "failed") && !is_migrating,
				ACTIONS,
			],
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
				__("Attach Public IPv4"),
				() => showAttachPublicIPDialog(frm, 4),
				!frm.doc.public_ipv4,
				ACTIONS,
			],
			[
				__("Detach Public IPv4"),
				() => detachPublicIP(frm, 4),
				Boolean(frm.doc.public_ipv4),
				ACTIONS,
			],
			[
				__("Attach Public IPv6"),
				() => showAttachPublicIPDialog(frm, 6),
				!frm.doc.public_ipv6 && !frm.doc.routed_ipv6,
				ACTIONS,
			],
			[
				__("Detach Public IPv6"),
				() => detachPublicIP(frm, 6),
				Boolean(frm.doc.public_ipv6 || frm.doc.routed_ipv6),
				ACTIONS,
			],
			[
				frm.doc.is_accessible_via_wireguard_gateway
					? __("Disable WG Gateway Access")
					: __("Enable WG Gateway Access"),
				() => setWireGuardGatewayAccess(frm),
				!frm.doc.is_network_gateway,
				ACTIONS,
			],
			[__("Edit Routes"), () => showRoutesDialog(frm), true, ACTIONS],
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
				frm.doc.is_network_gateway
					? __("Stop Network Gateway")
					: __("Make Network Gateway"),
				() => showNetworkGatewayDialog(frm),
				Boolean(frm.doc.is_privileged),
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
	openEditFirewallDialog(frm, JSON.parse(frm.doc.firewall_summary));
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

function showAttachPublicIPDialog(frm, version) {
	frappe.prompt(
		{
			fieldname: "allocation",
			fieldtype: "Data",
			label: __("Allocation"),
			reqd: 1,
			default: "auto",
			description: __("Use auto or the UUID of a reserved direct allocation."),
		},
		({ allocation }) =>
			frm
				.call({
					method: "attach_public_ip",
					doc: frm.doc,
					args: { version, allocation },
					freeze: true,
					freeze_message: __("Storing public IP intent..."),
				})
				.then(() => frm.reload_doc()),
		__("Attach Public IPv{0}", [version]),
		__("Attach")
	);
}

function detachPublicIP(frm, version) {
	frappe.confirm(
		__("Detach public IPv{0} from this Virtual Machine? Active connections can stop.", [
			version,
		]),
		() =>
			frm
				.call({
					method: "detach_public_ip",
					doc: frm.doc,
					args: { version },
					freeze: true,
					freeze_message: __("Storing public IP intent..."),
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

function showRoutesDialog(frm) {
	frm.call({
		method: "read_routes",
		doc: frm.doc,
		type: "GET",
		freeze: true,
		freeze_message: __("Reading routes..."),
	}).then(({ message: routes }) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Routes"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					options: `<p class="text-muted small">${__(
						"A VM without routes reaches only the mesh. 0.0.0.0/0 via host reaches the IPv4 internet. Active connections can stop."
					)}</p>`,
				},
				{
					fieldname: "routes",
					fieldtype: "Table",
					label: __("Routes"),
					cannot_add_rows: false,
					in_place_edit: true,
					data: routes || [],
					fields: [
						{
							fieldname: "destination",
							fieldtype: "Data",
							label: __("Destination"),
							in_list_view: 1,
							reqd: 1,
							description: __("0.0.0.0/0, 2000::/3, or a range such as fdac::/16."),
						},
						{
							fieldname: "via",
							fieldtype: "Data",
							label: __("Via"),
							in_list_view: 1,
							reqd: 1,
							description: __(
								"host for the host uplink, or the WireGuard mesh IPv6 address of a gateway VM."
							),
						},
						{
							fieldname: "gateway_virtual_machine",
							fieldtype: "Link",
							options: "Virtual Machine",
							label: __("Gateway VM"),
							in_list_view: 1,
							read_only: 1,
							description: __("The known owner of this address, when available."),
						},
					],
				},
			],
			primary_action_label: __("Save"),
			primary_action(values) {
				const routes = (values.routes || []).map((route) => ({
					destination: route.destination,
					via: route.via,
				}));
				frm.call({
					method: "set_routes",
					doc: frm.doc,
					args: { routes },
					freeze: true,
				}).then(() => {
					dialog.hide();
					frm.reload_doc();
				});
			},
		});
		dialog.show();
	});
}

function showNetworkGatewayDialog(frm) {
	const message = frm.doc.is_network_gateway
		? __("Stop {0} from carrying traffic for other virtual machines?", [frm.doc.name.bold()])
		: __(
				"Let {0} carry traffic for other virtual machines? It may then send any source address.",
				[frm.doc.name.bold()]
		  );

	frappe.confirm(message, () =>
		frm
			.call({
				method: "set_network_gateway",
				doc: frm.doc,
				args: { is_network_gateway: !frm.doc.is_network_gateway },
				freeze: true,
			})
			.then(() => frm.reload_doc())
	);
}

function setWireGuardGatewayAccess(frm) {
	const is_enabled = !frm.doc.is_accessible_via_wireguard_gateway;
	const message = is_enabled
		? __("Let customer devices on this tenant's WireGuard gateways reach {0}?", [
				frm.doc.name.bold(),
		  ])
		: __("Stop WireGuard gateway access to {0}?", [frm.doc.name.bold()]);

	frappe.confirm(message, () =>
		frm
			.call({
				method: "set_wireguard_gateway_access",
				doc: frm.doc,
				args: { is_enabled },
				freeze: true,
			})
			.then(() => frm.reload_doc())
	);
}

function rescueRequest(operation, generation) {
	switch (operation) {
		case "protected":
			return { enabled: true, access: "protected" };
		case "read_only":
			return { enabled: true, access: "read_only" };
		case "unprotected":
			return { enabled: true, access: "unprotected", allow_unprotected: true };
		case "keep":
			return { enabled: false };
		case "rollback":
			return { enabled: false, action: "rollback", recovery_generation: generation };
		case "finish":
			return { enabled: false, action: "finish", recovery_generation: generation };
		default:
			throw new Error("Invalid recovery operation");
	}
}

function rescueWarning(request) {
	let warning = __(
		"A running VM restarts. A stopped VM stays stopped. Rescue starts with the original disk unmounted."
	);
	if (request.action === "rollback") {
		warning = __(
			"Rollback permanently loses every original-disk write since this checkpoint, including writes made during normal boot. It cannot undo changes in external databases or services. The VM stops before restoration. Continue?"
		);
	} else if (request.action === "finish") {
		warning = __(
			"Keep the current disk and permanently delete this recovery checkpoint? Undo will no longer be available."
		);
	} else if (!request.enabled) {
		warning = __(
			"Keep original-disk changes and discard the temporary rescue disk? Copy its files out first and wait for repair jobs to finish. A protected session retains undo for 12 hours from its first exit; re-entry does not restart that window. If the original deadline has passed, this exit deletes the checkpoint."
		);
	} else if (request.allow_unprotected) {
		warning = __(
			"You are explicitly allowing original-disk writes WITHOUT a recovery checkpoint. Repairs can cause permanent data loss and cannot be rolled back. Continue without undo?"
		);
	}
	return warning;
}

function rescueOperationOptions(state) {
	const selected = state.desired.rescue.enabled;
	const recovery = state.desired.recovery || {};
	const labels = {
		protected: __("Enter protected rescue"),
		read_only: __("Enter read-only rescue"),
		unprotected: __("Enter rescue without undo"),
		keep: __("Keep changes and exit rescue"),
		rollback: __("Roll back to the recovery checkpoint"),
		finish: __("Finish recovery"),
	};
	let operations = [selected ? "keep" : "protected"];
	if (!selected && !recovery.checkpoint) operations.push("read_only", "unprotected");
	if (recovery.checkpoint) operations.push("rollback");
	if (recovery.checkpoint && !recovery.active) operations.push("finish");
	if (recovery.action && !recovery.complete) operations = [recovery.action];
	return [
		{ value: "", label: "" },
		...operations.map((value) => ({ value, label: labels[value] })),
	];
}

function rescueChangePending(state) {
	const recovery = state.desired.recovery || {};
	return (
		state.desired.rescue_generation !== state.observed.rescue_generation ||
		Boolean(recovery.action && !recovery.complete)
	);
}

function rescueHeading(state) {
	if (state.observed.error) return __("Recovery needs attention");
	if (rescueChangePending(state)) return __("Applying recovery change");
	if (!state.desired.rescue.enabled) return __("Normal boot is selected");
	return state.observed.state === "running" ? __("Rescue is running") : __("Rescue is selected");
}

function rescueProtection(recovery) {
	if (recovery.complete)
		return recovery.rollback_applied
			? __("Rollback completed; checkpoint removed.")
			: __("Recovery finished; changes kept and checkpoint removed.");
	let protection = recovery.checkpoint
		? __("Recovery checkpoint recorded. Its identity is checked before rollback.")
		: __("No recovery checkpoint available.");
	if (recovery.expires_at) {
		protection +=
			" " + __("Original expiry: {0}.", [new Date(recovery.expires_at).toLocaleString()]);
		if (recovery.active)
			protection +=
				" " +
				__(
					"Protection remains until this rescue session ends, even if that deadline passes."
				);
	}
	return protection;
}

function rescueStatusHTML(state) {
	const escape = (value) => frappe.utils.escape_html(String(value || ""));
	const error = state.observed.error;
	return `<p><strong>${escape(rescueHeading(state))}</strong></p>
		<p>${escape(rescueProtection(state.desired.recovery || {}))}</p>
		<p>${escape(
			__("Access: {0}. Host state: {1}. Phase: {2}.", [
				state.desired.rescue.access || __("normal"),
				state.observed.state,
				state.observed.phase || "—",
			])
		)}</p>
		${error ? `<p class="text-danger">${escape(error.message)}</p>` : ""}
		<p>${escape(
			__(
				"A checkpoint preserves the disk as rescue began; it is not a known-good backup. VM running does not mean Pilot or your sites are healthy."
			)
		)}</p>
		<p>${escape(
			__(
				"Connect through the console or your existing VM SSH keys. Run atlas-rescue status to inspect actual mounts and repair jobs."
			)
		)}</p>`;
}

function showRescueDialog(frm) {
	let state;
	let refreshing = false;
	let submitting = false;
	let timer;
	let closed = false;
	const dialog = new frappe.ui.Dialog({
		title: __("Rescue and recovery"),
		fields: [
			{ fieldtype: "HTML", fieldname: "status" },
			{
				fieldtype: "Select",
				fieldname: "operation",
				label: __("Choose an action"),
				options: "",
				reqd: 1,
			},
			{
				fieldtype: "Button",
				fieldname: "console",
				label: __("Open console"),
				click: () => openConsole(frm, "tty"),
			},
			{
				fieldtype: "Button",
				fieldname: "retry",
				label: __("Retry status"),
				hidden: 1,
				click: () => refresh().then(startPolling),
			},
		],
		primary_action_label: __("Continue"),
		primary_action: submit,
	});
	function updateSubmitButton() {
		const pending = state && rescueChangePending(state) && !state.observed.error;
		dialog.get_primary_btn().prop("disabled", Boolean(!state || pending || submitting));
	}
	function submit(values) {
		if (!state || submitting || closed) return;
		if (rescueChangePending(state) && !state.observed.error) return;
		if (
			!rescueOperationOptions(state).some(
				(option) => option.value === values.operation && option.value
			)
		)
			return;
		const request = rescueRequest(values.operation, state.desired.recovery?.generation);
		submitting = true;
		updateSubmitButton();
		frappe.confirm(
			rescueWarning(request),
			() => {
				if (closed) return;
				return Promise.resolve(
					frm.call({ method: "set_rescue", doc: frm.doc, args: request, freeze: true })
				)
					.then(() => {
						frappe.show_alert({
							message: __(
								"Recovery request accepted. Check progress before proceeding."
							),
							indicator: "blue",
						});
						frm.reload_doc();
					})
					.finally(() => {
						submitting = false;
						return refresh();
					});
			},
			() => {
				submitting = false;
				updateSubmitButton();
			}
		);
	}
	function startPolling() {
		if (state && !closed && !timer) timer = setInterval(refresh, 3000);
	}
	function refresh() {
		if (refreshing || closed) return Promise.resolve();
		refreshing = true;
		return Promise.resolve(frm.call({ method: "read_rescue", doc: frm.doc, type: "GET" }))
			.then(({ message }) => {
				if (!message) throw new Error("Missing recovery state");
				state = message;
				dialog.set_df_property("retry", "hidden", 1);
				dialog.fields_dict.status.$wrapper.html(rescueStatusHTML(state));
				const previous = dialog.get_value("operation");
				const options = rescueOperationOptions(state);
				dialog.set_df_property("operation", "options", options);
				dialog.set_value(
					"operation",
					options.some((option) => option.value === previous) ? previous : ""
				);
				updateSubmitButton();
			})
			.catch(() => {
				state = undefined;
				clearInterval(timer);
				timer = undefined;
				dialog.set_df_property("retry", "hidden", 0);
				updateSubmitButton();
				dialog.fields_dict.status.$wrapper.html(
					`<p class="text-danger">${frappe.utils.escape_html(
						__(
							"Recovery state is unavailable. Select Retry status to reconnect. Actions remain disabled until the read succeeds."
						)
					)}</p>`
				);
			})
			.finally(() => {
				refreshing = false;
			});
	}
	refresh().then(() => {
		dialog.show();
		startPolling();
		dialog.onhide = () => {
			closed = true;
			clearInterval(timer);
		};
	});
}
