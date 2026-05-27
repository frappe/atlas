const SCRIPT_FORMS = {
	"bootstrap-server.sh": {
		intro: "Idempotent. Safe to re-run on an Active server.",
		fields: [
			{
				fieldname: "FIRECRACKER_VERSION",
				label: "Firecracker Version",
				fieldtype: "Data",
				default: "v1.15.1",
				reqd: 1,
			},
			{
				fieldname: "ARCHITECTURE",
				label: "Architecture",
				fieldtype: "Select",
				options: ["x86_64", "aarch64"].join("\n"),
				default: "x86_64",
				reqd: 1,
			},
		],
	},
	"reboot-server.sh": {
		intro: "Reboots the host. SSH drops mid-Task; the Task may end Failure — that is normal.",
		fields: [],
	},
	"sync-image.sh": {
		intro: "Downloads kernel + rootfs from the image URLs onto the server.",
		fields: [
			{
				fieldname: "IMAGE_NAME",
				label: "Image",
				fieldtype: "Link",
				options: "Virtual Machine Image",
				reqd: 1,
				only_select: 1,
				get_query: () => ({filters: {is_active: 1}}),
			},
		],
	},
};


frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		add_buttons(frm);
		render_running_task_headline(frm);
		render_recent_tasks(frm);
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


function render_recent_tasks(frm) {
	const wrapper_id = "atlas-server-recent-tasks";
	frm.dashboard.wrapper?.find(`#${wrapper_id}`).remove();
	frappe.db.get_list("Task", {
		fields: ["name", "subject", "script", "status", "modified"],
		filters: {server: frm.doc.name},
		order_by: "modified desc",
		limit: 5,
	}).then((rows) => {
		if (!rows.length) return;
		const list = rows.map((row) => {
			const title = row.subject || row.script || row.name;
			// comment_when() returns a full HTML <span> with relative-time
			// tooltip; embed it directly (do not html-escape).
			const ago = frappe.datetime.comment_when(row.modified);
			return `<li class="d-flex align-items-center mb-1" style="gap: 0.5em;">
				<span class="indicator-pill ${indicator_color(row.status)}">${frappe.utils.escape_html(row.status || "—")}</span>
				<a href="/app/task/${encodeURIComponent(row.name)}" class="flex-grow-1">${frappe.utils.escape_html(title)}</a>
				<span class="text-muted small">${ago}</span>
			</li>`;
		}).join("");
		const html = `
			<div id="${wrapper_id}" class="form-section">
				<div class="section-head text-uppercase text-muted small mb-2">${__("Recent Tasks")}</div>
				<ul class="list-unstyled">${list}</ul>
				<a href="/app/task/view/list?server=${encodeURIComponent(frm.doc.name)}" class="small">${__("View all")} →</a>
			</div>
		`;
		frm.dashboard.add_section(html, "atlas-recent-tasks");
	});
}


function indicator_color(status) {
	return {
		Pending: "orange",
		Running: "yellow",
		Success: "green",
		Failure: "red",
	}[status] || "grey";
}


function subscribe_to_realtime(frm) {
	if (frm._atlas_server_realtime_registered) return;
	frm._atlas_server_realtime_registered = true;
	frappe.realtime.on("task_update", (data) => {
		if (!data || data.server !== frm.doc.name) return;
		render_running_task_headline(frm);
		render_recent_tasks(frm);
	});
}


function add_buttons(frm) {
	const status = frm.doc.status;
	if (["Pending", "Bootstrapping", "Broken"].includes(status)) {
		frappe.atlas.add_primary(frm, "Bootstrap", () => confirm_bootstrap(frm));
	} else {
		frappe.atlas.add_action(frm, "Re-bootstrap", () => confirm_bootstrap(frm));
	}
	frappe.atlas.add_action(frm, "Run Task", () => open_run_task_dialog(frm));
	frappe.atlas.add_danger(frm, "Reboot", () => confirm_reboot(frm));
}


function confirm_bootstrap(frm) {
	frappe.confirm(__("Bootstrap {0}?", [frm.doc.name]), () => {
		frm.call("bootstrap").then(({message}) => {
			frappe.atlas.task_started(frm, "Bootstrap", message);
		});
	});
}


function confirm_reboot(frm) {
	frappe.db.count("Virtual Machine", {
		filters: {server: frm.doc.name, status: "Running"},
	}).then((running_count) => {
		const body = `
			<p>${__("This will reboot {0}.", [`<b>${frappe.utils.escape_html(frm.doc.name)}</b>`])}</p>
			<p>${__("Running virtual machines: {0}. All will lose connectivity until the host returns.", [`<b>${running_count}</b>`])}</p>
			<p>${__("SSH will drop mid-Task — the reboot Task may end Status = Failure. That is normal.")}</p>
		`;
		frappe.atlas.confirm_destructive({
			title: __("Reboot {0}?", [frm.doc.name]),
			body_html: body,
			match_string: frm.doc.name,
			match_label: __("Type the server name to confirm"),
			proceed_label: __("Reboot"),
			proceed() {
				frm.call("reboot").then(({message}) => {
					frappe.atlas.task_started(frm, "Reboot", message);
				});
			},
		});
	});
}


function open_run_task_dialog(frm) {
	frm.call("get_scripts").then(({message: scripts}) => {
		const dialog = build_run_task_dialog(frm, scripts);
		dialog.show();
	});
}


function build_run_task_dialog(frm, scripts) {
	const is_system_manager = (frappe.user_roles || []).includes("System Manager");
	const all_per_script_field_names = collect_all_per_script_field_names();

	const fields = [
		{
			fieldname: "script",
			label: __("Script"),
			fieldtype: "Select",
			options: scripts.join("\n"),
			reqd: 1,
			onchange() {
				const script = dialog.get_value("script");
				refresh_script_intro(dialog, script);
				toggle_script_fields(dialog, script, all_per_script_field_names);
			},
		},
		{fieldname: "script_intro", fieldtype: "HTML"},
	];

	for (const [script, form] of Object.entries(SCRIPT_FORMS)) {
		for (const field of form.fields) {
			fields.push({
				...field,
				depends_on: `eval:doc.script === ${JSON.stringify(script)}`,
				mandatory_depends_on: field.reqd
					? `eval:doc.script === ${JSON.stringify(script)}`
					: undefined,
				reqd: 0,
			});
		}
	}

	if (is_system_manager) {
		fields.push(
			{fieldname: "show_advanced", label: __("Show advanced (System Manager)"), fieldtype: "Check"},
			{
				fieldname: "_advanced_variables",
				label: __("Variables (raw JSON)"),
				fieldtype: "Code",
				options: "JSON",
				depends_on: "eval:doc.show_advanced",
				description: __("Posted verbatim. Use only for debugging."),
				default: "{}",
			},
		);
	}

	const dialog = new frappe.ui.Dialog({
		title: __("Run Task"),
		fields: fields,
		primary_action_label: __("Run"),
		primary_action(values) {
			const script = values.script;
			let variables;
			if (is_system_manager && values.show_advanced) {
				variables = values._advanced_variables || "{}";
			} else {
				variables = collect_typed_variables(script, values);
			}
			frm.call("run_task_dialog", {script, variables}).then(({message: task_name}) => {
				dialog.hide();
				frappe.atlas.task_started(frm, script, task_name);
			});
		},
	});

	if (scripts.length === 1) {
		dialog.set_value("script", scripts[0]);
		refresh_script_intro(dialog, scripts[0]);
		toggle_script_fields(dialog, scripts[0], all_per_script_field_names);
	}

	return dialog;
}


function collect_all_per_script_field_names() {
	const names = new Set();
	for (const form of Object.values(SCRIPT_FORMS)) {
		for (const field of form.fields) {
			names.add(field.fieldname);
		}
	}
	return names;
}


function refresh_script_intro(dialog, script) {
	const form = SCRIPT_FORMS[script];
	const field = dialog.fields_dict.script_intro;
	if (!field || !field.$wrapper) return;
	if (!form || !form.intro) {
		field.$wrapper.empty();
		return;
	}
	field.$wrapper.html(
		`<div class="text-muted small">ⓘ ${frappe.utils.escape_html(form.intro)}</div>`,
	);
}


function toggle_script_fields(dialog, _script, _all_field_names) {
	// `depends_on` on each per-script field does the show/hide automatically
	// once we ask the dialog to re-evaluate dependencies.
	if (typeof dialog.refresh_dependency === "function") {
		dialog.refresh_dependency();
	}
}


function collect_typed_variables(script, values) {
	const form = SCRIPT_FORMS[script];
	if (!form) return "{}";
	const variables = {};
	for (const field of form.fields) {
		const value = values[field.fieldname];
		if (value !== undefined && value !== null && value !== "") {
			variables[field.fieldname] = value;
		}
	}
	return variables;
}
