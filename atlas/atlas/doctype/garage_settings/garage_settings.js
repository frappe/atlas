frappe.ui.form.on("Garage Settings", {
    refresh(frm) {
        frappe.atlas.add_action(frm, "Apply Garage Layout", () =>
            frappe.call({
                doc: frm.doc,
                method: "apply_layout",
                freeze: true,
                freeze_message: "Applying garage layout...",
            }).then(() => frm.reload_doc())
        );
    }
});
frappe.ui.form.on("Garage Settings", {
    refresh(frm) {
        frappe.atlas.add_action(frm, "Reconfigure all garages", () =>
            frappe.call({
                doc: frm.doc,
                method: "reconfigure_all_garages",
                freeze: true,
                freeze_message: "Reconfiguring garages...",
            }).then(() => frm.reload_doc())
        );
    }
});
