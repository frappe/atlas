frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		add_buttons(frm);
	},
});


function add_buttons(frm) {
	const status = frm.doc.status;
	if (status === "Archived") {
		return;
	}
	if (["Pending", "Bootstrapping", "Broken"].includes(status)) {
		frappe.atlas.add_primary(frm, "Bootstrap", () => confirm_bootstrap(frm));
	} else {
		frappe.atlas.add_success(frm, "Re-bootstrap", () => confirm_bootstrap(frm));
	}
	if (status === "Active") {
		frappe.atlas.add_action(frm, "Sync Image", () => open_sync_image_dialog(frm));
	}
	frappe.atlas.add_action(frm, "Archive", () => confirm_archive(frm));
	frappe.atlas.add_danger(frm, "Reboot", () => confirm_reboot(frm));
}


function confirm_bootstrap(frm) {
	frappe.confirm(__("Bootstrap {0}?", [frm.doc.title]), () => {
		frm.call("bootstrap").then(({message}) => {
			frappe.atlas.task_started(frm, "Bootstrap Server", message);
		});
	});
}


function confirm_reboot(frm) {
	frappe.atlas.confirm_destructive({
		title: __("Reboot {0}?", [frm.doc.title]),
		body_html: "",
		match_string: frm.doc.title,
		match_label: __("Type the server title to confirm"),
		proceed_label: __("Reboot"),
		proceed() {
			frm.call("reboot").then(({message}) => {
				frappe.atlas.task_started(frm, "Reboot", message);
			});
		},
	});
}


function confirm_archive(frm) {
	frappe.atlas.confirm_archive(frm, {
		match: frm.doc.title,
		match_label: __("Type the server title to confirm"),
		alert_message: __("Server archived."),
		body_html: `<p>${__("Atlas will destroy the vendor resource. This is irreversible.")}</p>`,
	});
}


function open_sync_image_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Sync Image"),
		fields: [{
			fieldname: "image",
			label: __("Image"),
			fieldtype: "Link",
			options: "Virtual Machine Image",
			reqd: 1,
			only_select: 1,
			get_query: () => ({filters: {is_active: 1}}),
		}],
		primary_action_label: __("Sync"),
		primary_action(values) {
			frm.call("sync_image", {image: values.image}).then(({message: task_name}) => {
				dialog.hide();
				frappe.atlas.task_started(frm, "Sync Image", task_name);
			});
		},
	});
	dialog.show();
}
