// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Investigation", {
	refresh(frm) {
		// The reference is text, so that the record can still be deleted. Show it as a link.
		frm.fields_dict.entries.grid.update_docfield_property(
			"reference_name",
			"formatter",
			(value, df, options, row) =>
				value && row?.reference_doctype
					? frappe.utils.get_form_link(row.reference_doctype, value, true)
					: frappe.utils.escape_html(value || "")
		);
		frm.refresh_field("entries");

		if (frm.doc.flow_session) {
			frm.add_custom_button(__("Open Chat"), () =>
				window.open(`/flow/s/${frm.doc.flow_session}`)
			);
		}
	},
});
