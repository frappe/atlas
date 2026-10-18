const SET_PAUSED =
	"atlas.vm.doctype.virtual_machine_migration.virtual_machine_migration.set_vm_migration_paused";

frappe.listview_settings["Virtual Machine Migration"] = {
	onload(listview) {
		showPauseButton(listview);
	},
};

function showPauseButton(listview) {
	frappe.db.get_single_value("Atlas Settings", "is_vm_migration_paused").then((is_paused) => {
		const label = is_paused ? __("Resume VM Migrations") : __("Pause VM Migrations");
		listview.page.add_inner_button(label, () => {
			frappe.call(SET_PAUSED, { is_paused: !is_paused }).then(() => {
				listview.page.remove_inner_button(label);
				showPauseButton(listview);
			});
		});
		listview.page.set_indicator(
			is_paused ? __("Migrations Paused") : "",
			is_paused ? "orange" : ""
		);
	});
}
