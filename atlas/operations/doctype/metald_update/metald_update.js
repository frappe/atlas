// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metald Update", {
	refresh(frm) {
		if (frm.doc.status !== "Draft") frm.disable_save();
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;

		const status = frm.doc.status;

		// A string action calls that method on the document. A function asks first.
		[
			[__("Plan Rollout"), "plan", status === "Draft"],
			[__("Start Rollout"), "start", status === "Planned"],
			[__("Edit Plan"), "edit_plan", status === "Planning" || status === "Planned"],
			[__("Pause Rollout"), "pause", status === "Running"],
			[__("Resume Rollout"), "resume", status === "Paused"],
			[
				__("Cancel Rollout"),
				() => cancelRollout(frm),
				status === "Running" || status === "Paused",
			],
		].forEach(([label, action, condition]) => {
			if (!condition) {
				return;
			}

			const call = () =>
				frm
					.call({ method: action, doc: frm.doc, freeze: true })
					.then(() => frm.reload_doc());

			frm.add_custom_button(label, typeof action === "string" ? call : action);
		});
	},
});

function cancelRollout(frm) {
	frappe.confirm(__("Cancel this rollout? Running server jobs finish."), () =>
		frm.call({ method: "cancel", doc: frm.doc, freeze: true }).then(() => frm.reload_doc())
	);
}
