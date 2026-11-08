frappe.ui.form.on("Runway Cache Server", {
	refresh(frm) {
		if (frm.doc.virtual_machine && frm.doc.status !== "Archived") {
			frm.add_custom_button(__("Re-provision"), () =>
				frm.call("reprovision").then(() => frm.reload_doc())
			);
			frm.add_custom_button(__("Archive"), () =>
				frappe.confirm(__("Terminate the cache VM and remove its route?"), () =>
					frm.call("archive").then(() => frm.reload_doc())
				)
			);
			return;
		}
		frm.add_custom_button(__("Provision"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Provision Runway Cache Server"),
				fields: [
					{
						fieldname: "virtual_machine_image",
						fieldtype: "Link",
						label: __("Image"),
						options: "Virtual Machine Image",
						reqd: 1,
						filters: { enabled: 1, status: "Available", image_type: "system" },
					},
					{
						fieldname: "cpu_millicores",
						fieldtype: "Int",
						label: __("CPU (millicores)"),
						default: 2000,
						reqd: 1,
					},
					{
						fieldname: "memory_mib",
						fieldtype: "Int",
						label: __("Memory (MiB)"),
						default: 4096,
						reqd: 1,
					},
					{
						fieldname: "disk_mib",
						fieldtype: "Int",
						label: __("Disk (MiB)"),
						default: 32768,
						reqd: 1,
					},
				],
				primary_action_label: __("Provision"),
				primary_action(values) {
					frm.call({
						method: "provision",
						args: { request: values },
						freeze: true,
					}).then(() => {
						dialog.hide();
						frm.reload_doc();
					});
				},
			});
			dialog.show();
		});
	},
});
