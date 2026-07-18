// SSH Console — the operator's ad-hoc fan-out command box.
//
// Execute (Shift+Enter) enqueues a background fan-out; per-target results stream
// back over the `ssh_console_update` realtime event, keyed by a per-run nonce so
// a stale form ignores a previous run's events. A toast links to the saved
// SSH Command Log when the run finishes.

frappe.ui.form.on("SSH Console", {
	onload(frm) {
		frappe.ui.keys.add_shortcut({
			shortcut: "shift+enter",
			action: () => frm.page.btn_primary && frm.page.btn_primary.trigger("click"),
			page: frm.page,
			description: __("Execute SSH Command"),
			ignore_inputs: true,
		});
		prefill_from_handoff(frm);
	},

	refresh(frm) {
		frm.disable_save();
		frm.page.set_primary_action(__("Execute"), ($btn) => execute(frm, $btn));
		subscribe(frm);
	},
});

function execute(frm, $btn) {
	if (!(frm.doc.command || "").trim()) {
		frappe.show_alert({ message: __("Enter a command to run."), indicator: "orange" });
		return;
	}
	if (!(frm.doc.targets || []).length) {
		frappe.show_alert({ message: __("Add at least one target."), indicator: "orange" });
		return;
	}
	frm.set_value("nonce", frappe.utils.get_random(16));
	frm.clear_table("results");
	frm.refresh_field("results");
	$btn.text(__("Executing..."));
	return frm
		.call({ doc: frm.doc, method: "execute" })
		.then((r) => {
			const log = r.message && r.message.log;
			if (log) {
				frappe.show_alert({
					message: __("Running — {0}", [
						`<a href="/app/ssh-command-log/${log}">${__("view log")}</a>`,
					]),
					indicator: "blue",
				});
			}
		})
		.finally(() => $btn.text(__("Execute")));
}

function subscribe(frm) {
	frappe.realtime.off("ssh_console_update");
	frappe.realtime.on("ssh_console_update", (message) => {
		if (message.nonce !== frm.doc.nonce) {
			return;
		}
		frm.set_value("results", message.results || []);
		if (message.status && message.status !== "Running") {
			const indicator = message.status === "Success" ? "green" : "red";
			frappe.show_alert({ message: __("Finished: {0}", [message.status]), indicator }, 5);
		}
	});
}

// When the operator arrives via a form's "Run Command" action or a log's
// "Re-run", a prefill payload was stashed in localStorage: a list of targets and
// (for Re-run) the command. Adopt it and clear the handoff so a later visit is
// clean.
function prefill_from_handoff(frm) {
	const raw = window.localStorage.getItem("ssh_console_prefill");
	if (!raw) {
		return;
	}
	window.localStorage.removeItem("ssh_console_prefill");
	let prefill;
	try {
		prefill = JSON.parse(raw);
	} catch (e) {
		return;
	}
	const targets = (prefill && prefill.targets) || [];
	if (!targets.length) {
		return;
	}
	frm.clear_table("targets");
	targets.forEach((target) => {
		if (target.target_doctype && target.target_name) {
			frm.add_child("targets", {
				target_doctype: target.target_doctype,
				target_name: target.target_name,
			});
		}
	});
	frm.refresh_field("targets");
	if (prefill.command) {
		frm.set_value("command", prefill.command);
	}
}
