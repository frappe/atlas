function sync_server_images(listview) {
	frappe.call({
		method: "run_doc_method",
		args: {
			dt: "Atlas Settings",
			method: "sync_server_images",
		},
		freeze: true,
		freeze_message: __("Queuing sync..."),
	}).then(() => listview.refresh());
}

frappe.listview_settings["Server Image"] = {
	refresh: function (listview) {
		if (!has_common(frappe.user_roles, ["System Manager"])) return;

		listview.page.add_inner_button(__("Sync"), function () {
			sync_server_images(listview);
		});
	},
};
