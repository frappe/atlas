frappe.ui.form.on("Virtual Machine Image", {
	refresh(frm) {
		if (frm.doc.image_type !== "Machine" || frm.doc.status !== "Failed") return;

		frm.add_custom_button(__("Retry Transfer"), () => {
			frm.call("retry_transfer").then(() => frm.reload_doc());
		});
	},
});
