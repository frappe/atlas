// SSH Command Log — the immutable record of one ad-hoc SSH Console run.
//
// Read-only. "Re-run" stashes this log's command + targets in localStorage and
// routes to the SSH Console, which adopts them on load (the same handoff the
// per-form "Run Command" action uses).

frappe.ui.form.on("SSH Command Log", {
	refresh(frm) {
		frm.disable_save();
		if (frm.doc.status === "Running") {
			return;
		}
		frm.add_custom_button(__("Re-run"), () => re_run(frm));
	},
});

function re_run(frm) {
	const targets = (frm.doc.results || []).map((row) => ({
		target_doctype: row.target_doctype,
		target_name: row.target_name,
	}));
	window.localStorage.setItem(
		"ssh_console_prefill",
		JSON.stringify({ command: frm.doc.command, targets })
	);
	frappe.set_route("Form", "SSH Console");
}
