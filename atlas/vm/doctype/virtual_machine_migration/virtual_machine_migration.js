const TERMINAL_STATUSES = ["completed", "failed", "aborted"];

frappe.ui.form.on("Virtual Machine Migration", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		// Atlas drives this record. A user acts through buttons, not a direct save.
		frm.disable_save();

		const is_active = !TERMINAL_STATUSES.includes(frm.doc.status);
		if (is_active) {
			showMigrationProgress(frm);
		}
		if (is_active && frm.doc.status !== "canceling") {
			frm.add_custom_button(__("Abort Migration"), () => abortMigration(frm), __("Actions"));
		}

		// Always called, so a finished migration also clears a running interval.
		scheduleProgressRefresh(frm);
	},
});

function showMigrationProgress(frm) {
	const progress = Math.min(100, Math.max(0, Number(frm.doc.progress_percent) || 0));
	const status = __(frappe.model.unscrub(frm.doc.status));
	frm.dashboard.show_progress(__("Migration Progress"), progress, status);
}

// A running migration reloads on an interval, so the desk shows live progress.
function scheduleProgressRefresh(frm) {
	if (frm.__migration_poll) {
		clearInterval(frm.__migration_poll);
		frm.__migration_poll = null;
	}
	if (TERMINAL_STATUSES.includes(frm.doc.status)) {
		return;
	}
	frm.__migration_poll = setInterval(() => {
		const route = frappe.get_route();
		const on_this_form =
			route[0] === "Form" && route[1] === frm.doctype && route[2] === frm.doc.name;
		if (!on_this_form) {
			clearInterval(frm.__migration_poll);
			frm.__migration_poll = null;
			return;
		}
		if (!frm.is_dirty()) {
			frm.reload_doc();
		}
	}, 2000);
}

function abortMigration(frm) {
	frappe.confirm(
		__(
			"Abort this migration? The VM stays on {0} if it has not cut over yet. Abort is not allowed once Atlas commits the VM to the destination.",
			[frm.doc.source_metal_server]
		),
		() =>
			frappe
				.call({
					// The worker saves this record on every poll, so send its name, not a stale copy.
					method: "run_doc_method",
					args: { dt: frm.doctype, dn: frm.doc.name, method: "abort" },
					freeze: true,
					freeze_message: __("Requesting abort..."),
				})
				.then(() => frm.reload_doc())
	);
}
