frappe.atlas = frappe.atlas || {};

frappe.atlas.add_primary = function (frm, label, fn) {
	const labelled = __(label);
	frm.add_custom_button(labelled, fn);
	// `change_custom_button_type` doesn't always promote a newly-added button
	// across every Frappe form-state transition (the Terminated VM path was a
	// regression). Apply the class directly via `frm.custom_buttons` — same
	// shape Desk uses for delete buttons in the right rail.
	const $btn = frm.custom_buttons && frm.custom_buttons[labelled];
	if ($btn && $btn.removeClass && $btn.addClass) {
		$btn.removeClass("btn-default").addClass("btn-primary");
	} else {
		frm.change_custom_button_type(labelled, null, "primary");
	}
	// One primary per page: Desk paints Save as solid `.btn-primary` on every
	// refresh, which competes with the Atlas lifecycle hero. Demote Save to
	// outline for this refresh cycle — the next `frm.page.set_primary_action`
	// call will re-promote it on the no-primary path.
	const $save = frm.page && frm.page.btn_primary;
	if ($save && $save.length) {
		$save.removeClass("btn-primary").addClass("btn-default");
	}
};

frappe.atlas.add_secondary = function (frm, label, fn) {
	frm.add_custom_button(__(label), fn);
};

frappe.atlas.add_action = function (frm, label, fn) {
	frm.add_custom_button(__(label), fn, __("Actions"));
};

frappe.atlas.add_danger = function (frm, label, fn) {
	const labelled = __(label);
	frm.add_custom_button(labelled, fn, __("Actions"));
	// Dropdown items aren't `.btn` elements, so `change_custom_button_type`'s
	// `btn-danger` class paints a full button-in-menu. Use `text-danger` on the
	// anchor itself — same convention Desk uses for the right-rail Delete.
	// `atlas-tonal-danger` (atlas_desk.css) paints the row red on hover.
	const $btn = frm.custom_buttons && frm.custom_buttons[labelled];
	if ($btn && $btn.addClass) {
		$btn.addClass("text-danger atlas-tonal-danger");
	}
};

frappe.atlas.add_success = function (frm, label, fn) {
	// Used for safe, primary-flavoured actions that live in the Actions
	// dropdown — e.g. `Re-bootstrap` / `Sync`. Paints the item green on
	// hover via the tonal class in atlas_desk.css.
	const labelled = __(label);
	frm.add_custom_button(labelled, fn, __("Actions"));
	const $btn = frm.custom_buttons && frm.custom_buttons[labelled];
	if ($btn && $btn.addClass) {
		$btn.addClass("atlas-tonal-success");
	}
};

frappe.atlas.confirm_cost = function ({ title, body_html, proceed_label, proceed }) {
	// Cost tier: not destructive, but spends real money / disk / bandwidth
	// (provision a billable server, copy a multi-GB rootfs, re-lay a disk).
	// Thin wrapper over `frappe.warn` so the orange Provision-style indicator
	// and copy live behind one signature the spec documents. Latency / size
	// hints live in `body_html`, supplied by the caller.
	frappe.warn(title, body_html || "", proceed, proceed_label || __("Proceed"), true);
};

frappe.atlas.confirm_destructive = function ({
	title,
	body_html,
	match_string,
	match_label,
	proceed_label,
	proceed,
}) {
	const dialog = new frappe.ui.Dialog({
		title: title,
		fields: [
			{ fieldname: "body", fieldtype: "HTML", options: body_html },
			{
				fieldname: "confirmation",
				label: match_label || __("Type to confirm"),
				fieldtype: "Data",
				reqd: 1,
				description: __("Type {0} to enable the button below.", [
					`<b>${frappe.utils.escape_html(match_string)}</b>`,
				]),
			},
		],
		primary_action_label: proceed_label || __("Proceed"),
		primary_action() {
			if (dialog.get_value("confirmation") !== match_string) {
				frappe.show_alert({
					message: __("Confirmation text does not match."),
					indicator: "orange",
				});
				return;
			}
			dialog.hide();
			proceed();
		},
	});
	dialog.show();
	const button = dialog.get_primary_btn();
	button.addClass("btn-danger").prop("disabled", true);
	dialog.fields_dict.confirmation.$input.on("input", () => {
		const matches = dialog.get_value("confirmation") === match_string;
		button.prop("disabled", !matches);
	});
	return dialog;
};

frappe.atlas.confirm_archive = function (frm, { match, match_label, alert_message, body_html }) {
	frappe.atlas.confirm_destructive({
		title: __("Archive {0}?", [match]),
		body_html: body_html || "",
		match_string: match,
		match_label: match_label,
		proceed_label: __("Archive"),
		proceed() {
			frm.call("archive").then(() => {
				frappe.show_alert({
					message: alert_message,
					indicator: "blue",
				});
				frm.reload_doc();
			});
		},
	});
};

frappe.atlas.task_started = function (frm, label, task_name) {
	frappe.show_alert(
		{
			message: __("{0} Task: {1}", [
				label,
				`<a href="/app/task/${encodeURIComponent(task_name)}">${frappe.utils.escape_html(
					task_name
				)}</a>`,
			]),
			indicator: "blue",
		},
		6
	);
	frappe.set_route("Form", "Task", task_name);
};

frappe.atlas.strip_desk_chrome = function (frm) {
	if (frm.page && frm.page.sidebar) {
		frm.page.sidebar.hide();
	}
	const $body = frm.page && frm.page.wrapper;
	if ($body && $body.find) {
		$body.find(".layout-main-section-wrapper").removeClass("col-lg-8").addClass("col-lg-12");
		// Hide the timeline + comments wrappers. Frappe varies the wrapping
		// markup across versions — hide every known shape so the Comments
		// section doesn't leak below the form.
		$body
			.find(
				[
					".form-timeline",
					".new-timeline",
					".timeline",
					".comment-input-container",
					".comment-input-wrapper",
					".comment-input-placeholder",
					".comment-box",
					".comment-box-container",
					".form-comments",
					".comments",
					".timeline-content",
				].join(", ")
			)
			.hide();
	}
};

for (const doctype of [
	"Server",
	"Provider",
	"Atlas Settings",
	"DigitalOcean Settings",
	"Self-Managed Settings",
	"Provider Size",
	"Provider Image",
	"Virtual Machine",
	"Virtual Machine Image",
	"Virtual Machine Snapshot",
	"Reserved IP",
	"Task",
	"Domain Provider",
	"Route53 Settings",
	"TLS Provider",
	"Lets Encrypt Settings",
	"Root Domain",
	"TLS Certificate",
	"Central Settings",
	"Central Size",
	"Central Image",
]) {
	frappe.ui.form.on(doctype, {
		onload(frm) {
			frappe.atlas.strip_desk_chrome(frm);
		},
		refresh(frm) {
			frappe.atlas.strip_desk_chrome(frm);
		},
	});
}
