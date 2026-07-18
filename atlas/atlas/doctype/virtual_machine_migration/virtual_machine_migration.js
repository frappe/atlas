// Virtual Machine Migration — a read-mostly form: the scheduler drives the phase
// machine (spec/19 §7), so the operator watches progress and, on a Failed row,
// clicks Retry. There are no per-phase manual buttons — the lifecycle guard
// blocks concurrent VM actions while a migration runs, so the row alone is the
// control surface.

frappe.ui.form.on("Virtual Machine Migration", {
	refresh(frm) {
		if (frm.is_new()) return;
		render_progress(frm);
		add_retry_button(frm);
		subscribe_to_realtime(frm);
	},
});

// The phase order, mirrored from migration.PHASE_ORDER, for a simple progress
// read-out. Done/Failed are terminal and handled separately.
const PHASES = [
	"Pending",
	"ExportingSnapshot",
	"TargetPreparing",
	"InjectingIdentity",
	"Hydrating",
	"CutoverStarting",
	"Repointing",
	"Cleanup",
	"Done",
];

function render_progress(frm) {
	frm.set_intro("");
	const status = frm.doc.status;

	if (status === "Failed") {
		const at = frm.doc.error_at_status
			? __(" (failed at {0})", [frm.doc.error_at_status])
			: "";
		frm.set_intro(
			__("Migration failed{0}. Fix the cause, then click Retry to resume from that phase.", [
				at,
			]),
			"red"
		);
		return;
	}

	if (status === "Done") {
		if (frm.doc.keep_address) {
			frm.set_intro(
				__(
					"Done — the VM kept its address; traffic is now forwarded from the source host until collapsed. Manage the forward from the Virtual Machine."
				),
				"green"
			);
		} else {
			frm.set_intro(
				__("Done — the VM moved to a new address and the proxy was re-pointed."),
				"green"
			);
		}
		return;
	}

	// In-flight: lead with the always-current progress_detail line (finer than the
	// phase name — it says which host and step), then the step counter, a percent
	// bar for any measurable copy (base-image ship or disk hydration), and the
	// tunnel state on the keep-address path.
	const index = PHASES.indexOf(status);
	const step = index >= 0 ? `${index + 1}/${PHASES.length}` : "";
	const detail = frm.doc.progress_detail || __("phase {0}", [status]);
	let message = __("Step {0} — {1}", [step, detail]);
	const percent = frm.doc.progress_percent;
	if (percent != null && percent >= 0) {
		message += render_bar(percent);
	}
	if (frm.doc.keep_address && frm.doc.tunnel_status) {
		message += __(" — tunnel {0}", [frm.doc.tunnel_status]);
	}
	frm.set_intro(message, "blue");
}

// A tiny inline progress bar (no dependency on frappe's ProgressBar widget, which
// lives on the dashboard, not the intro). Percent is already clamped 0–100 on the
// server; render defensively anyway.
function render_bar(percent) {
	const p = Math.max(0, Math.min(100, percent));
	return (
		` <span style="display:inline-block;vertical-align:middle;width:120px;height:8px;` +
		`background:var(--gray-200);border-radius:4px;overflow:hidden;margin-left:6px;">` +
		`<span style="display:block;height:100%;width:${p}%;background:var(--blue-500);"></span>` +
		`</span> ${p}%`
	);
}

function add_retry_button(frm) {
	if (frm.doc.status !== "Failed") return;
	frappe.atlas.add_primary(frm, __("Retry"), () => {
		frm.call("retry").then(() => {
			frappe.show_alert(
				{
					message: __("Retrying — the scheduler will resume the phase."),
					indicator: "blue",
				},
				5
			);
			frm.reload_doc();
		});
	});
}

function subscribe_to_realtime(frm) {
	if (frm._atlas_migration_realtime_registered) return;
	frm._atlas_migration_realtime_registered = true;
	// The scheduler advances the row on its own cadence; a live doctype update
	// nudges the form so the operator sees phase/hydration move without a manual
	// refresh. Guarded to this row.
	frappe.realtime.on("doc_update", (data) => {
		if (data && data.doctype === "Virtual Machine Migration" && data.name === frm.doc.name) {
			frm.reload_doc();
		}
	});
}
