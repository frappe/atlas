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
	},
});


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
