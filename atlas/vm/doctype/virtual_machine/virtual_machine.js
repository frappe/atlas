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

		frm.add_custom_button(
			__("Create Machine Image"),
			() => showCreateMachineImageDialog(frm),
			__("Actions")
		);
		frm.add_custom_button(__("Resize Disk"), () => showResizeDiskDialog(frm), __("Actions"));
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

function showCreateMachineImageDialog(frm) {
	frappe.prompt(
		[
			{ fieldname: "title", fieldtype: "Data", label: __("Image Title"), reqd: 1 },
			{ fieldname: "cache_image", fieldtype: "Check", label: __("Cache Image") },
			{ fieldname: "memory_snapshot", fieldtype: "Check", label: __("Memory Snapshot") },
			{
				fieldname: "memory_snapshot_virtual_cpu_count",
				fieldtype: "Int",
				label: __("Memory Snapshot vCPUs"),
				default: frm.doc.vcpus,
				depends_on: "eval:doc.memory_snapshot",
				mandatory_depends_on: "eval:doc.memory_snapshot",
			},
			{
				fieldname: "memory_snapshot_memory_mib",
				fieldtype: "Int",
				label: __("Memory Snapshot Memory (MiB)"),
				default: frm.doc.memory_mib,
				depends_on: "eval:doc.memory_snapshot",
				mandatory_depends_on: "eval:doc.memory_snapshot",
			},
			{
				fieldname: "memory_snapshot_disk_mib",
				fieldtype: "Int",
				label: __("Memory Snapshot Disk (MiB)"),
				default: frm.doc.disk_mib,
				depends_on: "eval:doc.memory_snapshot",
				mandatory_depends_on: "eval:doc.memory_snapshot",
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
