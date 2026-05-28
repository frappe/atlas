const PRIMARY_BY_STATUS = {
	Failed: {label: "Provision", method: "provision"},
	Stopped: {label: "Start", method: "start"},
	Running: {label: "Stop", method: "stop"},
};

const SECONDARY_BY_STATUS = {
	Running: [{label: "Restart", method: "restart"}],
	Stopped: [{label: "Restart", method: "restart"}],
};


const SIZE_PRESETS = {
	"Small (1 vCPU / 512 MB / 4 GB)": {vcpus: 1, memory_megabytes: 512, disk_gigabytes: 4},
	"Medium (2 vCPU / 2048 MB / 10 GB)": {vcpus: 2, memory_megabytes: 2048, disk_gigabytes: 10},
	"Large (4 vCPU / 8192 MB / 40 GB)": {vcpus: 4, memory_megabytes: 8192, disk_gigabytes: 40},
};


frappe.ui.form.on("Virtual Machine", {
	onload(frm) {
		if (frm.is_new()) {
			auto_select_server(frm);
		}
	},
	refresh(frm) {
		if (frm.is_new()) {
			render_creation_messages(frm);
			return;
		}
		add_lifecycle_buttons(frm);
		add_terminated_actions(frm);
		render_dashboard_chips(frm);
		render_status_intro(frm);
		expand_networking_for_pending(frm);
		subscribe_to_realtime(frm);
	},
	size_preset(frm) {
		const preset = SIZE_PRESETS[frm.doc.size_preset];
		if (!preset) return;
		frm.set_value("vcpus", preset.vcpus);
		frm.set_value("memory_megabytes", preset.memory_megabytes);
		frm.set_value("disk_gigabytes", preset.disk_gigabytes);
	},
	server(frm) {
		if (frm.is_new()) {
			render_creation_messages(frm);
		}
	},
	vcpus(frm) {
		if (frm.is_new()) {
			render_creation_messages(frm);
		}
	},
});


function auto_select_server(frm) {
	if (frm.doc.server) return;
	frappe.db.get_list("Server", {
		filters: {status: "Active"},
		pluck: "name",
		limit: 2,
	}).then((rows) => {
		if (rows.length === 1) {
			frm.set_value("server", rows[0]);
		}
	});
}


function render_creation_messages(frm) {
	frm._atlas_creation_render_token = (frm._atlas_creation_render_token || 0) + 1;
	const token = frm._atlas_creation_render_token;
	frm.dashboard.clear_headline();
	if (frm.doc.server) {
		render_capacity_indicator(frm, token);
	}
}


function render_capacity_indicator(frm, token) {
	// Only surface the headline when the host is oversubscribed — green/blue
	// "you have headroom" banners crowd the form without adding signal.
	frappe.call({
		method: "atlas.atlas.api.server_capacity.capacity_for_server",
		args: {server: frm.doc.server},
	}).then(({message}) => {
		if (!message) return;
		if (token !== frm._atlas_creation_render_token) return;
		const total = message.total_vcpus;
		const used = message.used_vcpus;
		const requested = parseInt(frm.doc.vcpus || 0, 10);
		const projected = used + requested;
		if (total == null || projected <= total) {
			return;
		}
		const text = __(
			"Server is oversubscribed: {0} requested + {1} used / {2} total ({3} VMs). Provision may fail.",
			[requested, used, total, message.virtual_machine_count],
		);
		frm.dashboard.set_headline_alert(`⚠ ${text}`, "red");
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
	frappe.atlas.add_danger(frm, "Terminate", () => confirm_terminate(frm));
}


function add_terminated_actions(frm) {
	if (frm.doc.status !== "Terminated") return;
	frappe.atlas.add_primary(frm, "Re-provision as new", () => reprovision_as_new(frm));
	frappe.atlas.add_danger(frm, "Delete record", () => confirm_delete(frm));
}


function confirm_lifecycle(frm, action) {
	frappe.confirm(__("{0} {1}?", [action.label, frm.doc.title || frm.doc.name.slice(0, 8)]), () => {
		frm.call(action.method).then(({message: task_name}) => {
			if (typeof task_name === "string") {
				frappe.atlas.task_started(frm, action.label, task_name);
			} else {
				frm.reload_doc();
			}
		});
	});
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
			frm.call("terminate").then(({message: task_name}) => {
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
	frappe.show_alert({
		message: __("New Virtual Machine prefilled. Review and Save to insert."),
		indicator: "blue",
	}, 5);
}


function render_dashboard_chips(frm) {
	frm.dashboard.clear_headline?.();
	if (frm.doc.ipv6_address) {
		const safe = frappe.utils.escape_html(frm.doc.ipv6_address);
		const indicator_color =
			frm.doc.status === "Running" ? "green" :
			frm.doc.status === "Pending" ? "orange" :
			frm.doc.status === "Failed" ? "red" : "grey";
		frm.dashboard.add_indicator(`IPv6 [${safe}]`, indicator_color);
	}
}


function render_status_intro(frm) {
	frm.set_intro("");
	const status = frm.doc.status;

	if (status === "Terminated") {
		const when_html = frm.doc.last_stopped
			? frappe.datetime.comment_when(frm.doc.last_stopped)
			: `<span>${__("earlier")}</span>`;
		frm.dashboard.set_headline_alert(
			`⛔ ${__("Terminated")} ${when_html}. ${__("This record is kept for audit; the VM no longer exists.")}`,
			"red",
		);
		return;
	}

	if (status === "Failed" || status === "Pending") {
		frappe.db.get_list("Task", {
			fields: ["name", "subject", "status", "modified", "script"],
			filters: {
				virtual_machine: frm.doc.name,
				status: "Failure",
				script: "provision-vm.sh",
			},
			order_by: "modified desc",
			limit: 1,
		}).then((rows) => {
			if (!rows.length) return;
			const failure = rows[0];
			const subject = failure.subject || failure.name;
			const link = `<a href="/app/task/${encodeURIComponent(failure.name)}">${frappe.utils.escape_html(subject)} →</a>`;
			frm.set_intro(
				__("Last Provision attempt failed — {0}. Fix the cause, then click Provision to retry.", [link]),
				"red",
			);
		});
	}
}


function expand_networking_for_pending(frm) {
	if (frm.doc.status !== "Pending" || !frm.doc.ipv6_address) return;
	const section = (cur_frm?.layout?.sections || []).find(
		(s) => s.df && s.df.fieldname === "section_break_networking",
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
