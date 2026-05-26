frappe.ui.form.on("Virtual Machine", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const status = frm.doc.status;
		const allowed = {
			Pending:    ["provision", "terminate"],
			Running:    ["stop", "restart", "terminate"],
			Stopped:    ["start", "restart", "terminate"],
			Failed:     ["provision", "terminate"],
			Terminated: [],
		}[status] ?? [];

		const buttons = {
			provision: ["Provision", "provision"],
			start:     ["Start", "start"],
			stop:      ["Stop", "stop"],
			restart:   ["Restart", "restart"],
			terminate: ["Terminate", "terminate"],
		};
		for (const [action, [label, method]] of Object.entries(buttons)) {
			if (!allowed.includes(action)) continue;
			frm.add_custom_button(label, () => {
				frappe.confirm(`${label} ${frm.doc.name}?`, () => {
					frm.call(method).then(() => frm.reload_doc());
				});
			});
		}
	},
});
