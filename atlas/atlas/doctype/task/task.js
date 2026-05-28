const HEADLINE_BY_STATUS = {
	Pending: {color: "blue", text: "Queued — waiting for worker."},
	Running: {color: "yellow", text: "Running"},
	Success: {color: "green", text: "Completed"},
	Failure: {color: "red", text: "Failed"},
};


frappe.ui.form.on("Task", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.disable_save();
		render_headline(frm);
		add_retry_button(frm);
		pretty_print_variables(frm);
		subscribe_to_realtime(frm);
	},
	onload(frm) {
		frm._atlas_realtime_registered = false;
	},
});


function render_headline(frm) {
	const status = frm.doc.status;
	const config = HEADLINE_BY_STATUS[status];
	if (!config) return;

	let text = config.text;
	const duration = describe_duration(frm.doc.duration_milliseconds);
	if (status === "Running") {
		const started = frappe.datetime.comment_when(frm.doc.started);
		text = `${config.text} on ${frappe.utils.escape_html(frm.doc.server || "—")} — started ${started}.`;
	} else if (status === "Success") {
		text = `Completed in ${duration}. Exit code ${frm.doc.exit_code ?? 0}.`;
	} else if (status === "Failure") {
		text = `Failed in ${duration}. Exit code ${frm.doc.exit_code ?? "—"}.`;
	}
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline_alert(text, config.color);
}


function describe_duration(milliseconds) {
	if (!milliseconds) return "—";
	const seconds = Math.round(milliseconds / 1000);
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	const remainder = seconds % 60;
	return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}


function add_retry_button(frm) {
	if (frm.doc.status !== "Failure") return;
	frappe.atlas.add_primary(frm, "Retry", () => {
		frappe.confirm(__("Retry this Task?"), () => {
			frm.call("retry").then(({message: task_name}) => {
				frappe.atlas.task_started(frm, "Retry", task_name);
			});
		});
	});
}


function pretty_print_variables(frm) {
	const raw = frm.doc.variables;
	if (!raw || frm._atlas_variables_prettified === frm.doc.name) return;
	let parsed;
	try {
		parsed = JSON.parse(raw);
	} catch (e) {
		return;
	}
	const pretty = JSON.stringify(parsed, null, 2);
	if (pretty === raw) {
		frm._atlas_variables_prettified = frm.doc.name;
		return;
	}
	frm.doc.variables = pretty;
	frm.refresh_field("variables");
	frm._atlas_variables_prettified = frm.doc.name;
}


function subscribe_to_realtime(frm) {
	if (frm._atlas_realtime_registered) return;
	frm._atlas_realtime_registered = true;
	frappe.realtime.on("task_update", (data) => {
		if (!data || data.name !== frm.doc.name) return;
		frm.reload_doc();
	});
}
