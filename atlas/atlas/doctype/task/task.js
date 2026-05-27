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
		render_chips(frm);
		add_retry_button(frm);
		render_sibling_tasks(frm);
		enlarge_log_panes(frm);
		subscribe_to_realtime(frm);
	},
	onload(frm) {
		// One global subscription per form lifecycle. Realtime updates fire
		// before refresh in some cases, so we register early.
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
		const first_line = first_stderr_line(frm.doc.stderr);
		text = `Failed in ${duration}. Exit code ${frm.doc.exit_code ?? "—"}.`;
		if (first_line) {
			text += `<br><span class="text-muted small">${frappe.utils.escape_html(first_line)}</span>`;
		}
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


function first_stderr_line(stderr) {
	if (!stderr) return "";
	return stderr
		.split("\n")
		.map((l) => l.trim())
		.filter((l) => l && !l.startsWith("+ "))
		.slice(0, 1)[0] || "";
}


function render_chips(frm) {
	frm.dashboard.clear_headline_indicators?.();
	const dashboard = frm.dashboard;
	if (!dashboard || !dashboard.add_indicator) return;
	if (frm.doc.server) {
		dashboard.add_indicator(
			`Server: ${frappe.utils.escape_html(frm.doc.server)}`,
			"blue",
		);
	}
	if (frm.doc.virtual_machine) {
		frappe.db.get_value("Virtual Machine", frm.doc.virtual_machine, "description")
			.then(({message}) => {
				const description = message?.description || frm.doc.virtual_machine.slice(0, 8);
				dashboard.add_indicator(
					`VM: ${frappe.utils.escape_html(description)}`,
					"blue",
				);
			});
	}
	if (frm.doc.triggered_by) {
		dashboard.add_indicator(
			`Triggered by ${frappe.utils.escape_html(frm.doc.triggered_by)}`,
			"grey",
		);
	}
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


function render_sibling_tasks(frm) {
	const subject_field = frm.fields_dict.subject;
	if (!subject_field) return;

	const filter = frm.doc.virtual_machine
		? {virtual_machine: frm.doc.virtual_machine}
		: frm.doc.server
			? {server: frm.doc.server}
			: null;
	if (!filter) return;

	filter.name = ["!=", frm.doc.name];

	frappe.db.get_list("Task", {
		fields: ["name", "subject", "status", "modified"],
		filters: filter,
		order_by: "modified desc",
		limit: 5,
	}).then((rows) => {
		if (!rows.length) return;
		const list = rows.map((row) => {
			const ago = frappe.datetime.comment_when(row.modified);
			const status_label = row.status || "—";
			const title = row.subject || row.name;
			return `<li>
				<span class="indicator-pill ${indicator_class(row.status)}">${frappe.utils.escape_html(status_label)}</span>
				<a href="/app/task/${encodeURIComponent(row.name)}">${frappe.utils.escape_html(title)}</a>
				<span class="text-muted small">${frappe.utils.escape_html(ago)}</span>
			</li>`;
		}).join("");
		const html = `
			<div class="form-section atlas-sibling-tasks">
				<div class="section-head text-uppercase text-muted small mb-2">${__("Sibling tasks")}</div>
				<ul class="list-unstyled">${list}</ul>
			</div>
		`;
		frm.dashboard.add_section(html, "sibling-tasks");
	});
}


function indicator_class(status) {
	return {
		Pending: "orange",
		Running: "yellow",
		Success: "green",
		Failure: "red",
	}[status] || "grey";
}


function enlarge_log_panes(frm) {
	for (const fieldname of ["stdout", "stderr"]) {
		const field = frm.fields_dict[fieldname];
		if (!field) continue;
		// Make the Code field roomier; Frappe wires `min_height` to the
		// underlying CodeMirror/textarea wrapper.
		try {
			frm.set_df_property(fieldname, "options", "Text");
			field.$wrapper.find("textarea, .CodeMirror").css({"min-height": "24em"});
		} catch (_) {
			// Best-effort enlargement; we don't fail the form refresh on it.
		}
	}
}


function subscribe_to_realtime(frm) {
	if (frm._atlas_realtime_registered) return;
	frm._atlas_realtime_registered = true;
	frappe.realtime.on("task_update", (data) => {
		if (!data || data.name !== frm.doc.name) return;
		frm.reload_doc();
	});
}
