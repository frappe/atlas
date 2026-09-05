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

		if (is_running || is_paused || current_state === "created") {
			frm.add_custom_button(
				__("TTY Console"),
				() => openConsole(frm, "tty"),
				__("Console Access")
			);
		}
		if (is_running) {
			frm.add_custom_button(
				__("SSH Console"),
				() => openConsole(frm, "ssh"),
				__("Console Access")
			);
		}

		[
			[__("Start VM"), "start", is_stopped, __("Starting...")],
			[__("Stop VM"), "stop", is_running || is_paused, __("Stopping...")],
			[__("Reboot VM"), "reboot", is_running, __("Rebooting...")],
			[__("Pause VM"), "pause", is_running, __("Pausing...")],
			[__("Resume VM"), "resume", is_paused, __("Resuming...")],
		].forEach(([label, method, condition, freeze_message]) => {
			if (!condition) {
				return;
			}
			frm.add_custom_button(
				label,
				() =>
					frm
						.call({ method, doc: frm.doc, freeze: true, freeze_message })
						.then(() => frm.reload_doc()),
				__("Actions")
			);
		});

		frm.add_custom_button(
			__("Snapshot VM"),
			() => showCreateMachineImageDialog(frm),
			__("Actions")
		);
		frm.add_custom_button(__("Resize Disk"), () => showResizeDiskDialog(frm), __("Actions"));
		frm.add_custom_button(
			__("Edit Disk Limits"),
			() => showEditDiskLimitsDialog(frm),
			__("Actions")
		);
		if (is_stopped) {
			frm.add_custom_button(
				__("Resize Compute"),
				() => showResizeComputeDialog(frm),
				__("Actions")
			);
		}
		frm.add_custom_button(
			__("Edit SSH Keys"),
			() => showEditSSHKeysDialog(frm),
			__("Actions")
		);
		frm.add_custom_button(
			__("Edit Metadata"),
			() => showEditMetadataDialog(frm),
			__("Actions")
		);
		frm.add_custom_button(
			__("Edit Network Throughput"),
			() => showEditThroughputDialog(frm),
			__("Actions")
		);
		frm.add_custom_button(
			__("Change Egress Mode"),
			() => showEgressDialog(frm),
			__("Actions")
		);
		if (frm.doc.public_ipv4) {
			frm.add_custom_button(
				__("Detach IP Address"),
				() => detachIPAddress(frm),
				__("Actions")
			);
		} else {
			frm.add_custom_button(
				__("Attach IP Address"),
				() => showAttachIPAddressDialog(frm),
				__("Actions")
			);
		}
		frm.add_custom_button(
			__("Terminate VM"),
			() =>
				frm
					.call({
						method: "terminate",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Requesting termination..."),
					})
					.then(() => frm.reload_doc()),
			__("Dangerous Actions")
		);
	},
});

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
		const url =
			"/vm_console?vm=" +
			encodeURIComponent(frm.doc.name) +
			"#token=" +
			encodeURIComponent(token);
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

function showResizeComputeDialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "vcpus",
				fieldtype: "Int",
				label: __("vCPUs"),
				reqd: 1,
				default: frm.doc.vcpus,
			},
			{
				fieldname: "memory_mib",
				fieldtype: "Int",
				label: __("Memory (MiB)"),
				reqd: 1,
				default: frm.doc.memory_mib,
			},
		],
		({ vcpus, memory_mib }) =>
			frm
				.call({
					method: "resize_compute",
					doc: frm.doc,
					args: { vcpus, memory_mib },
					freeze: true,
					freeze_message: __("Resizing compute..."),
				})
				.then(() => frm.reload_doc()),
		__("Resize Compute"),
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
			label: __("Server IP Address"),
			options: "Server IP Address",
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
