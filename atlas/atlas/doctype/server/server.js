frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		add_buttons(frm);
		render_running_task_headline(frm);
		subscribe_to_realtime(frm);
	},
});


function render_running_task_headline(frm) {
	frm.dashboard.clear_headline?.();
	frappe.db.get_list("Task", {
		fields: ["name", "subject", "script", "status", "started", "modified"],
		filters: {
			server: frm.doc.name,
			status: ["in", ["Pending", "Running"]],
		},
		order_by: "modified desc",
		limit: 1,
	}).then((rows) => {
		if (!rows.length) return;
		const task = rows[0];
		const subject = task.subject || task.script || task.name;
		const when_started_html = task.started
			? frappe.datetime.comment_when(task.started)
			: `<span class="text-muted small">${__("just now")}</span>`;
		const link = `<a href="/app/task/${encodeURIComponent(task.name)}">${frappe.utils.escape_html(subject)} →</a>`;
		frm.dashboard.set_headline_alert(
			`⏵ ${__("Running task")}: ${link} <span class="text-muted small">${when_started_html}</span>`,
			"yellow",
		);
	});
}


function subscribe_to_realtime(frm) {
	if (frm._atlas_server_realtime_registered) return;
	frm._atlas_server_realtime_registered = true;
	frappe.realtime.on("task_update", (data) => {
		if (!data || data.server !== frm.doc.name) return;
		render_running_task_headline(frm);
	});
}


function add_buttons(frm) {
	const status = frm.doc.status;
	if (status === "Archived") {
		return;
	}
	if (["Pending", "Bootstrapping", "Broken"].includes(status)) {
		frappe.atlas.add_primary(frm, "Bootstrap", () => confirm_bootstrap(frm));
	} else {
		frappe.atlas.add_success(frm, "Re-bootstrap", () => confirm_bootstrap(frm));
	}
	if (status === "Active") {
		frappe.atlas.add_action(frm, "Sync Image", () => open_sync_image_dialog(frm));
	}
	frappe.atlas.add_action(frm, "Archive", () => confirm_archive(frm));
	frappe.atlas.add_danger(frm, "Reboot", () => confirm_reboot(frm));
}


function confirm_bootstrap(frm) {
	frappe.confirm(__("Bootstrap {0}?", [frm.doc.title]), () => {
		frm.call("bootstrap").then(({message}) => {
			frappe.atlas.task_started(frm, "Bootstrap Server", message);
		});
	});
}


function confirm_reboot(frm) {
	frappe.atlas.confirm_destructive({
		title: __("Reboot {0}?", [frm.doc.title]),
		body_html: "",
		match_string: frm.doc.title,
		match_label: __("Type the server title to confirm"),
		proceed_label: __("Reboot"),
		proceed() {
			frm.call("reboot").then(({message}) => {
				frappe.atlas.task_started(frm, "Reboot", message);
			});
		},
	});
}


function confirm_archive(frm) {
	frappe.atlas.confirm_destructive({
		title: __("Archive {0}?", [frm.doc.title]),
		body_html: "",
		match_string: frm.doc.title,
		match_label: __("Type the server title to confirm"),
		proceed_label: __("Archive"),
		proceed() {
			frm.call("archive").then(() => {
				frappe.show_alert({
					message: __("Server archived."),
					indicator: "blue",
				});
				frm.reload_doc();
			});
		},
	});
}


function open_sync_image_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Sync Image"),
		fields: [{
			fieldname: "image",
			label: __("Image"),
			fieldtype: "Link",
			options: "Virtual Machine Image",
			reqd: 1,
			only_select: 1,
			get_query: () => ({filters: {is_active: 1}}),
		}],
		primary_action_label: __("Sync"),
		primary_action(values) {
			frm.call("sync_image", {image: values.image}).then(({message: task_name}) => {
				dialog.hide();
				frappe.atlas.task_started(frm, "Sync Image", task_name);
			});
		},
	});
	dialog.show();
}
