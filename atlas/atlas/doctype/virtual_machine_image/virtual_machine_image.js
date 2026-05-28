frappe.ui.form.on("Virtual Machine Image", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		if (frm.doc.is_active) {
			frappe.atlas.add_danger(frm, "Archive", () => confirm_archive(frm));
		}
	},
});


function confirm_archive(frm) {
	const match = frm.doc.title || frm.doc.image_name;
	frappe.atlas.confirm_destructive({
		title: __("Archive {0}?", [match]),
		body_html: "",
		match_string: match,
		match_label: __("Type the image title to confirm"),
		proceed_label: __("Archive"),
		proceed() {
			frm.call("archive").then(() => {
				frappe.show_alert({
					message: __("Image archived."),
					indicator: "blue",
				});
				frm.reload_doc();
			});
		},
	});
}
