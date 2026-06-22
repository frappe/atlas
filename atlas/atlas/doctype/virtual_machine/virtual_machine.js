const PRIMARY_BY_STATUS = {
	Failed: { label: "Provision", method: "provision" },
	Stopped: { label: "Start", method: "start" },
	Running: { label: "Stop", method: "stop" },
	Paused: { label: "Resume", method: "resume" },
};

// Plain no-arg lifecycle methods invoked through confirm_lifecycle.
const SECONDARY_BY_STATUS = {
	Running: [
		{ label: "Restart", method: "restart" },
		{ label: "Pause", method: "pause" },
	],
	Stopped: [{ label: "Restart", method: "restart" }],
	Paused: [{ label: "Stop", method: "stop" }],
};

// Stopped-only actions that open a dialog or confirm (they take arguments or
// have consequences worth a prompt).
const DIALOG_ACTIONS_WHEN_STOPPED = [
	{ label: "Snapshot", handler: open_snapshot_dialog },
	{ label: "Rebuild", handler: open_rebuild_dialog },
	{ label: "Resize", handler: open_resize_dialog },
	{ label: "Regenerate host keys", handler: confirm_regenerate_host_keys },
];

// Five tiers — keep in sync with atlas/atlas/sizes.py SIZE_PRESETS (the
// canonical source) and the SPA's NewMachineDialog.vue. `vcpus` is the guest
// thread count; `cpu_max_cores` is the cgroup cpu.max bandwidth cap. "Shared Nx"
// are oversubscribable fractions of a core; "Dedicated 1x" is a full core.
// test_sizes.py pins these in sync.
const SIZE_PRESETS = {
	"Shared 1x": { vcpus: 1, cpu_max_cores: 0.0625, memory_megabytes: 512, disk_gigabytes: 10 },
	"Shared 2x": { vcpus: 1, cpu_max_cores: 0.125, memory_megabytes: 1024, disk_gigabytes: 20 },
	"Shared 4x": { vcpus: 1, cpu_max_cores: 0.25, memory_megabytes: 2048, disk_gigabytes: 40 },
	"Shared 8x": { vcpus: 1, cpu_max_cores: 0.5, memory_megabytes: 4096, disk_gigabytes: 80 },
	"Dedicated 1x": { vcpus: 1, cpu_max_cores: 1, memory_megabytes: 8192, disk_gigabytes: 160 },
};

frappe.ui.form.on("Virtual Machine", {
	onload(frm) {
		if (frm.is_new()) {
			auto_select_server(frm);
		}
	},
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		add_lifecycle_buttons(frm);
		add_terminated_actions(frm);
		render_status_intro(frm);
		expand_networking_for_pending(frm);
		subscribe_to_realtime(frm);
	},
	size_preset(frm) {
		const preset = SIZE_PRESETS[frm.doc.size_preset];
		if (!preset) return;
		frm.set_value("vcpus", preset.vcpus);
		frm.set_value("cpu_max_cores", preset.cpu_max_cores);
		frm.set_value("memory_megabytes", preset.memory_megabytes);
		frm.set_value("disk_gigabytes", preset.disk_gigabytes);
	},
});

function auto_select_server(frm) {
	if (frm.doc.server) return;
	frappe.db
		.get_list("Server", {
			filters: { status: "Active" },
			pluck: "name",
			limit: 2,
		})
		.then((rows) => {
			if (rows.length === 1) {
				frm.set_value("server", rows[0]);
			}
		});
}

function add_lifecycle_buttons(frm) {
	const status = frm.doc.status;
	if (status === "Terminated") {
		return;
	}
	const primary = PRIMARY_BY_STATUS[status];
	if (primary) {
		frappe.atlas.add_primary(frm, primary.label, () => confirm_lifecycle(frm, primary));
	}
	for (const action of SECONDARY_BY_STATUS[status] || []) {
		frappe.atlas.add_secondary(frm, action.label, () => confirm_lifecycle(frm, action));
	}
	if (status === "Stopped") {
		// Disk/size actions are rare and deliberate (each opens a dialog and
		// spends real disk + time) — they live under `Actions ▾`, not on the
		// top bar, so Start/Restart stay the visible siblings.
		for (const action of DIALOG_ACTIONS_WHEN_STOPPED) {
			frappe.atlas.add_action(frm, action.label, () => action.handler(frm));
		}
	}
	if (status === "Running" || status === "Paused") {
		// Live snapshot: no stop required. Crash-consistent (the dialog says so).
		frappe.atlas.add_action(frm, "Snapshot (live)", () =>
			open_snapshot_dialog(frm, { live: true })
		);
		// Warm snapshot: capture the live guest's memory AND disk at one paused
		// instant into a kind=Warm snapshot — no stop. Clones resume instead of
		// cold-booting (see capture_warm_snapshot() in virtual_machine.py).
		frappe.atlas.add_action(frm, "Warm snapshot", () => open_warm_snapshot_dialog(frm));
		// One-click fast stop: capture the guest's memory so the next Start
		// resumes in milliseconds instead of cold-booting. The one-off form of
		// the per-VM "Memory Snapshot on Stop" flag.
		frappe.atlas.add_action(frm, "Stop (memory snapshot)", () =>
			confirm_lifecycle(frm, {
				label: "Stop (memory snapshot)",
				method: "stop",
				args: { memory_snapshot: true },
			})
		);
	}
	frappe.atlas.add_danger(frm, "Terminate", () => confirm_terminate(frm));
}

function add_terminated_actions(frm) {
	if (frm.doc.status !== "Terminated") return;
	frappe.atlas.add_primary(frm, "Re-provision as new", () => reprovision_as_new(frm));
	frappe.atlas.add_danger(frm, "Delete record", () => confirm_delete(frm));
}

function confirm_lifecycle(frm, action) {
	frappe.confirm(
		__("{0} {1}?", [action.label, frm.doc.title || frm.doc.name.slice(0, 8)]),
		() => {
			// `args` is optional; most lifecycle methods take none. Stop (memory
			// snapshot) posts {memory_snapshot: true} to the same stop() method.
			frm.call(action.method, action.args).then(({ message: task_name }) => {
				if (typeof task_name === "string") {
					frappe.atlas.task_started(frm, action.label, task_name);
				} else {
					frm.reload_doc();
				}
			});
		}
	);
}

function confirm_terminate(frm) {
	const match = frm.doc.title || frm.doc.name;
	frappe.atlas.confirm_destructive({
		title: __("Terminate {0}?", [match]),
		body_html: "",
		match_string: match,
		match_label: __("Type the title to confirm"),
		proceed_label: __("Terminate"),
		proceed() {
			frm.call("terminate").then(({ message: task_name }) => {
				frappe.atlas.task_started(frm, "Terminate", task_name);
			});
		},
	});
}

function confirm_delete(frm) {
	const match = frm.doc.title || frm.doc.name;
	frappe.atlas.confirm_destructive({
		title: __("Delete record for {0}?", [match]),
		body_html: "",
		match_string: match,
		match_label: __("Type the title to confirm"),
		proceed_label: __("Delete record"),
		proceed() {
			frappe.db.delete_doc("Virtual Machine", frm.doc.name).then(() => {
				frappe.show_alert({
					message: __("Deleted {0}.", [match]),
					indicator: "green",
				});
				frappe.set_route("List", "Virtual Machine");
			});
		},
	});
}

function reprovision_as_new(frm) {
	const clone = frappe.new_doc("Virtual Machine", {
		server: frm.doc.server,
		image: frm.doc.image,
		vcpus: frm.doc.vcpus,
		memory_megabytes: frm.doc.memory_megabytes,
		disk_gigabytes: frm.doc.disk_gigabytes,
		ssh_public_key: frm.doc.ssh_public_key,
		title: frm.doc.title ? `${frm.doc.title} (clone)` : "",
	});
	if (clone && typeof clone.then === "function") {
		clone.then(() => maybe_alert_cloned());
	} else {
		maybe_alert_cloned();
	}
}

function maybe_alert_cloned() {
	frappe.show_alert(
		{
			message: __("New Virtual Machine prefilled. Review and Save to insert."),
			indicator: "blue",
		},
		5
	);
}

function open_snapshot_dialog(frm, { live = false } = {}) {
	// A live snapshot is an instant LVM thin copy of the running disks: no stop,
	// but crash-consistent (see snapshot() in virtual_machine.py). A normal
	// snapshot is taken from a Stopped VM and is flush-clean.
	const hint = live
		? __(
				"Snapshots the running VM in place — no stop. The image is <b>crash-consistent</b> (like a power-cut at this instant): unflushed writes may be missing and ext4 replays its journal on restore. Stop first if you need a guaranteed-clean snapshot."
		  )
		: __("Instant copy-on-write snapshot of the {0} GB rootfs (and the data disk, if any).", [
				frm.doc.disk_gigabytes,
		  ]);
	frappe.prompt(
		[
			{
				fieldname: "title",
				label: __("Snapshot title"),
				fieldtype: "Data",
				reqd: 1,
				default: frm.doc.title ? `${frm.doc.title} snapshot` : "",
			},
			{
				fieldname: "cost_hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${hint}</p>`,
			},
		],
		({ title }) => {
			frm.call("snapshot", { title, live }).then(({ message: snapshot_name }) => {
				frappe.show_alert(
					{
						message: __("Snapshot {0} created.", [snapshot_name]),
						indicator: "green",
					},
					6
				);
				frm.reload_doc();
			});
		},
		live
			? __("Live snapshot {0}", [frm.doc.title || frm.doc.name.slice(0, 8)])
			: __("Snapshot {0}", [frm.doc.title || frm.doc.name.slice(0, 8)]),
		__("Create snapshot")
	);
}

function open_warm_snapshot_dialog(frm) {
	// A warm snapshot pauses the live guest and captures its memory pair
	// (vmstate + mem) AND an LVM disk snapshot at the SAME instant into a durable
	// kind=Warm row, then resumes — the VM never stops (see
	// capture_warm_snapshot() in virtual_machine.py). Restoring it onto its own
	// VM is the fast resume shape; fanning it out into clones is only safe for a
	// golden baked with the in-guest freshen unit.
	const hint = __(
		"Pauses the running VM, captures its <b>memory + disk</b> at one instant into a warm snapshot, then resumes — no stop. Clones can <b>resume</b> from it instead of cold-booting. Root disk only (no data disk)."
	);
	frappe.prompt(
		[
			{
				fieldname: "title",
				label: __("Snapshot title"),
				fieldtype: "Data",
				reqd: 1,
				default: frm.doc.title ? `${frm.doc.title} warm` : "",
			},
			{
				fieldname: "cost_hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${hint}</p>`,
			},
		],
		({ title }) => {
			frm.call("capture_warm_snapshot", { title }).then(({ message: snapshot_name }) => {
				frappe.show_alert(
					{
						message: __("Warm snapshot {0} created.", [snapshot_name]),
						indicator: "green",
					},
					6
				);
				frm.reload_doc();
			});
		},
		__("Warm snapshot {0}", [frm.doc.title || frm.doc.name.slice(0, 8)]),
		__("Create warm snapshot")
	);
}

function confirm_regenerate_host_keys(frm) {
	const who = frm.doc.title || frm.doc.name.slice(0, 8);
	frappe.confirm(
		__(
			"Regenerate SSH host keys for {0}? This changes the VM's SSH identity — on next connect clients will see a changed host key and must clear the old entry (<code>ssh-keygen -R &lt;address&gt;</code>). Rebuild and restore deliberately keep the keys; only use this to rotate them on purpose.",
			[who]
		),
		() => {
			frm.call("regenerate_host_keys").then(({ message: task_name }) => {
				frappe.atlas.task_started(frm, "Regenerate host keys", task_name);
			});
		}
	);
}

function open_rebuild_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Rebuild {0}", [frm.doc.title || frm.doc.name.slice(0, 8)]),
		fields: [
			{
				fieldname: "source_type",
				label: __("Rebuild from"),
				fieldtype: "Select",
				options: [
					{ value: "image", label: __("Base image (fresh disk)") },
					{ value: "snapshot", label: __("A snapshot of this VM") },
				],
				default: "image",
				reqd: 1,
			},
			{
				fieldname: "image",
				label: __("Image"),
				fieldtype: "Link",
				options: "Virtual Machine Image",
				default: frm.doc.image,
				depends_on: "eval:doc.source_type == 'image'",
				description: __("Defaults to the current image. Wipes stored data."),
			},
			{
				fieldname: "snapshot",
				label: __("Snapshot"),
				fieldtype: "Link",
				options: "Virtual Machine Snapshot",
				depends_on: "eval:doc.source_type == 'snapshot'",
				get_query: () => ({
					filters: { virtual_machine: frm.doc.name, status: "Available" },
				}),
			},
			{
				fieldname: "cost_hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Swaps this VM's disk bytes in place — current data is overwritten. Takes up to a few minutes; the VM stays Stopped."
				)}</p>`,
			},
		],
		primary_action_label: __("Rebuild"),
		primary_action(values) {
			const source = values.source_type === "snapshot" ? values.snapshot : values.image;
			dialog.hide();
			frappe.atlas.confirm_cost({
				title: __("Rebuild {0}?", [frm.doc.title || frm.doc.name.slice(0, 8)]),
				body_html: `<p>${__("This overwrites the VM's disk and cannot be undone.")}</p>`,
				proceed_label: __("Rebuild"),
				proceed() {
					frm.call("rebuild", { source_type: values.source_type, source }).then(
						({ message: task_name }) =>
							frappe.atlas.task_started(frm, "Rebuild", task_name)
					);
				},
			});
		},
	});
	dialog.show();
}

function open_resize_dialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "vcpus",
				label: __("vCPUs"),
				fieldtype: "Int",
				default: frm.doc.vcpus,
				reqd: 1,
			},
			{
				fieldname: "memory_megabytes",
				label: __("Memory (MB)"),
				fieldtype: "Int",
				default: frm.doc.memory_megabytes,
				reqd: 1,
			},
			{
				fieldname: "disk_gigabytes",
				label: __("Disk (GB)"),
				fieldtype: "Int",
				default: frm.doc.disk_gigabytes,
				reqd: 1,
				description: __("Disk can only grow."),
			},
			{
				fieldname: "cost_hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Rewrites the Firecracker config and grows the rootfs — up to ~2 minutes. The VM stays Stopped."
				)}</p>`,
			},
		],
		(values) => {
			frm.call("resize", values).then(({ message: task_name }) =>
				frappe.atlas.task_started(frm, "Resize", task_name)
			);
		},
		__("Resize {0}", [frm.doc.title || frm.doc.name.slice(0, 8)]),
		__("Resize")
	);
}

function render_status_intro(frm) {
	frm.set_intro("");
	const status = frm.doc.status;

	if (status === "Terminated") {
		return;
	}

	if (status === "Failed" || status === "Pending") {
		frappe.db
			.get_list("Task", {
				fields: ["name", "subject", "status", "modified", "script"],
				filters: {
					virtual_machine: frm.doc.name,
					status: "Failure",
					script: "provision-vm.sh",
				},
				order_by: "modified desc",
				limit: 1,
			})
			.then((rows) => {
				if (!rows.length) return;
				const failure = rows[0];
				const subject = failure.subject || failure.name;
				const link = `<a href="/app/task/${encodeURIComponent(
					failure.name
				)}">${frappe.utils.escape_html(subject)} →</a>`;
				frm.set_intro(
					__(
						"Last Provision attempt failed — {0}. Fix the cause, then click Provision to retry.",
						[link]
					),
					"red"
				);
			});
	}
}

function expand_networking_for_pending(frm) {
	if (frm.doc.status !== "Pending" || !frm.doc.ipv6_address) return;
	const section = (frm?.layout?.sections || []).find(
		(s) => s.df && s.df.fieldname === "section_break_networking"
	);
	if (section && typeof section.collapse === "function") {
		section.collapse(false);
	}
}

function subscribe_to_realtime(frm) {
	if (frm._atlas_vm_realtime_registered) return;
	frm._atlas_vm_realtime_registered = true;
	frappe.realtime.on("virtual_machine_update", (data) => {
		if (!data || data.name !== frm.doc.name) return;
		frm.reload_doc();
	});
	frappe.realtime.on("task_update", (data) => {
		if (!data || data.virtual_machine !== frm.doc.name) return;
		frm.reload_doc();
	});
}
