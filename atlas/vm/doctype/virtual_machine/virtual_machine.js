frappe.ui.form.on("Virtual Machine", {
	refresh(frm) {
		if (frm.is_new()) {
			frappe.set_route("List", "Virtual Machine");
			return;
		}

		const current_state = frm.doc.current_state;
		const is_running = current_state === "running";
		const is_stopped = current_state === "stopped";
		const is_paused = current_state === "paused";

		[
			[__("Start"), "start", is_stopped, __("Starting...")],
			[__("Stop"), "stop", is_running || is_paused, __("Stopping...")],
			[__("Reboot"), "reboot", is_running, __("Rebooting...")],
			[__("Pause"), "pause", is_running, __("Pausing...")],
			[__("Resume"), "resume", is_paused, __("Resuming...")],
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

		frm.add_custom_button(
			__("Create Machine Image"),
			() => showCreateMachineImageDialog(frm),
			__("Actions")
		);
		frm.add_custom_button(__("Resize Disk"), () => showResizeDiskDialog(frm), __("Actions"));
		if (is_stopped) {
			frm.add_custom_button(
				__("Resize Compute"),
				() => showResizeComputeDialog(frm),
				__("Actions")
			);
		}
		frm.add_custom_button(
			__("Terminate"),
			() =>
				frm
					.call({
						method: "terminate",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Requesting termination..."),
					})
					.then(() => frm.reload_doc()),
			__("Actions")
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
