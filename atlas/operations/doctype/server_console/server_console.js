// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Server Console", {
	refresh(frm) {
		frm.disable_save();
		frm.page.set_primary_action(__("Execute"), () =>
			frm
				.call({ method: "execute", doc: frm.doc, freeze: true })
				.then(({ message }) => frappe.set_route("Form", "Server Console Log", message))
		);
		frm.add_custom_button(__("Logs"), () => frappe.set_route("List", "Server Console Log"));
		frm.add_custom_button(__("Pick Servers"), () => pickServers(frm));
		// Adding a button shows the menu again, so hide it last. Developers keep it to edit the DocType.
		if (!frappe.boot.developer_mode) frm.page.hide_menu();
	},
});

const PICKER_COLUMNS = ["title", "status", "public_ipv4_address", "private_ipv4_address"];

// The search box matches the name and the title. The list shows the title instead of the UUID name.
class ServerPicker extends frappe.ui.form.MultiSelectDialog {
	make() {
		super.make();
		this.dialog.set_df_property("search_term", "label", __("Title"));
		this.dialog.get_secondary_btn().addClass("hide");
		this.$results.removeClass("mt-2").css("border-radius", "8px");
	}

	get_datatable_columns() {
		return PICKER_COLUMNS;
	}

	get_args_for_search() {
		const args = super.get_args_for_search();
		args.filter_fields.push(...PICKER_COLUMNS);
		return args;
	}
}

function pickServers(frm) {
	const picker = new ServerPicker({
		doctype: "Metal Server",
		target: frm,
		setters: { status: "Running" },
		primary_action_label: __("Add to List"),
		action() {
			const entries = (frm.doc.servers || "").split(/[,\s]+/).filter(Boolean);
			const titles = picker.get_checked_items().map((server) => server.title);
			frm.set_value("servers", [...new Set([...entries, ...titles])].join(", "));
			picker.dialog.hide();
		},
	});
}
