frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button("Bootstrap", () => {
			frappe.confirm(`Bootstrap ${frm.doc.name}?`, () => {
				frm.call("bootstrap").then(({message}) => {
					frappe.show_alert({
						message: `Bootstrap Task: ${message}`,
						indicator: "blue",
					});
					frm.reload_doc();
				});
			});
		});
		frm.add_custom_button("Run Task", () => {
			open_run_task_dialog(frm);
		});
		frm.add_custom_button("Reboot", () => {
			frappe.confirm(
				`Reboot ${frm.doc.name}? SSH will drop; the Task will end Failure.`,
				() => {
					frm.call("reboot").then(({message}) => {
						frappe.show_alert({
							message: `Reboot Task: ${message}`,
							indicator: "orange",
						});
						frappe.set_route("Form", "Task", message);
					});
				},
			);
		});
		render_form_extras(frm);
	},
});


function open_run_task_dialog(frm) {
	frm.call("get_scripts").then(({message: scripts}) => {
		const dialog = new frappe.ui.Dialog({
			title: "Run Task",
			fields: [
				{
					fieldname: "script",
					label: "Script",
					fieldtype: "Select",
					options: (scripts || []).join("\n"),
					reqd: 1,
				},
				{
					fieldname: "variables",
					label: "Variables (JSON)",
					fieldtype: "Code",
					options: "JSON",
					default: "{}",
				},
			],
			primary_action_label: "Run",
			primary_action(values) {
				frm.call("run_task_dialog", {
					script: values.script,
					variables: values.variables,
				}).then(({message: task_name}) => {
					dialog.hide();
					frappe.set_route("Form", "Task", task_name);
				});
			},
		});
		dialog.show();
	});
}


function render_form_extras(frm) {
	frm.call("get_form_extras").then(({message: extras}) => {
		if (!extras) {
			return;
		}
		const vm_wrapper = frm.get_field("virtual_machines_html").$wrapper;
		vm_wrapper.html(render_virtual_machines(extras.virtual_machines || []));
		const task_wrapper = frm.get_field("recent_tasks_html").$wrapper;
		task_wrapper.html(render_recent_tasks(extras.recent_tasks || []));
	});
}


function render_virtual_machines(rows) {
	if (!rows.length) {
		return `<p class="text-muted">No virtual machines on this server.</p>`;
	}
	const body = rows.map((row) => `
		<tr>
			<td><a href="/app/virtual-machine/${frappe.utils.escape_html(row.name)}">${frappe.utils.escape_html(row.name)}</a></td>
			<td>${frappe.utils.escape_html(row.description || "")}</td>
			<td>${frappe.utils.escape_html(row.status || "")}</td>
			<td>${row.vcpus || ""}</td>
			<td>${row.memory_megabytes || ""}</td>
			<td>${frappe.utils.escape_html(row.ipv6_address || "")}</td>
		</tr>
	`).join("");
	return `
		<table class="table table-bordered">
			<thead>
				<tr>
					<th>Name</th>
					<th>Description</th>
					<th>Status</th>
					<th>vCPUs</th>
					<th>RAM (MB)</th>
					<th>IPv6</th>
				</tr>
			</thead>
			<tbody>${body}</tbody>
		</table>
	`;
}


function render_recent_tasks(rows) {
	if (!rows.length) {
		return `<p class="text-muted">No tasks for this server yet.</p>`;
	}
	const body = rows.map((row) => `
		<tr>
			<td><a href="/app/task/${frappe.utils.escape_html(row.name)}">${frappe.utils.escape_html(row.name)}</a></td>
			<td>${frappe.utils.escape_html(row.script || "")}</td>
			<td>${frappe.utils.escape_html(row.status || "")}</td>
			<td>${row.duration_milliseconds || ""}</td>
			<td>${frappe.utils.escape_html(row.creation || "")}</td>
		</tr>
	`).join("");
	return `
		<table class="table table-bordered">
			<thead>
				<tr>
					<th>Task</th>
					<th>Script</th>
					<th>Status</th>
					<th>Duration (ms)</th>
					<th>Created</th>
				</tr>
			</thead>
			<tbody>${body}</tbody>
		</table>
	`;
}
