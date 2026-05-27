const PRIMARY_BY_STATUS = {
	Pending: {label: "Provision", method: "provision"},
	Failed: {label: "Provision", method: "provision"},
	Stopped: {label: "Start", method: "start"},
	Running: {label: "Stop", method: "stop"},
};

const SECONDARY_BY_STATUS = {
	Running: [{label: "Restart", method: "restart"}],
	Stopped: [{label: "Restart", method: "restart"}],
};


frappe.ui.form.on("Virtual Machine", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		add_lifecycle_buttons(frm);
	},
});


function add_lifecycle_buttons(frm) {
	const status = frm.doc.status;
	const primary = PRIMARY_BY_STATUS[status];
	if (primary) {
		frappe.atlas.add_primary(frm, primary.label, () => confirm_lifecycle(frm, primary));
	}
	for (const action of SECONDARY_BY_STATUS[status] || []) {
		frappe.atlas.add_secondary(frm, action.label, () => confirm_lifecycle(frm, action));
	}
	if (status !== "Terminated") {
		frappe.atlas.add_danger(frm, "Terminate", () => confirm_terminate(frm));
	}
}


function confirm_lifecycle(frm, action) {
	frappe.confirm(__("{0} {1}?", [action.label, frm.doc.name.slice(0, 8)]), () => {
		frm.call(action.method).then(() => frm.reload_doc());
	});
}


function confirm_terminate(frm) {
	const short_id = frm.doc.name.slice(0, 8);
	const body = `
		<p>${__("IPv6: {0}", [`<code>${frappe.utils.escape_html(frm.doc.ipv6_address || "—")}</code>`])}</p>
		<p>${__("Image: {0}", [`<b>${frappe.utils.escape_html(frm.doc.image || "—")}</b>`])}</p>
		<p>${__("Server: {0}", [`<b>${frappe.utils.escape_html(frm.doc.server || "—")}</b>`])}</p>
		<p>${__("This deletes the VM's disk artifacts on the host. The UUID and Task history are preserved.")}</p>
	`;
	frappe.atlas.confirm_destructive({
		title: __("Terminate {0}?", [frm.doc.description || short_id]),
		body_html: body,
		match_string: short_id,
		match_label: __("Type the short ID ({0}) to confirm", [short_id]),
		proceed_label: __("Terminate"),
		proceed() {
			frm.call("terminate").then(({message: task_name}) => {
				frappe.atlas.task_started(frm, "Terminate", task_name);
			});
		},
	});
}
