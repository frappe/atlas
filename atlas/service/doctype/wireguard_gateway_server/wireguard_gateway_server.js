// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Wireguard Gateway Server", {
	refresh(frm) {
		frm.disable_save();
		if (!has_common(frappe.user_roles, ["System Manager"]) || frm.doc.status === "Archived") {
			return;
		}

		frm.add_custom_button(
			__("Archive"),
			() =>
				frappe.confirm(
					__(
						"Archive {0}? Its devices disconnect, and it leaves the regional gateway name. Remove its devices through the gateway API first.",
						[frm.doc.name]
					),
					() =>
						frm
							.call({ method: "archive", doc: frm.doc, freeze: true })
							.then(() => frm.reload_doc())
				),
			__("Dangerous Actions")
		);

		frm.add_custom_button(
			__("Rebuild"),
			() => {
				const dialog = new frappe.ui.Dialog({
					title: __("Rebuild {0}", [frm.doc.name]),
					fields: [
						{
							fieldname: "public_ipv4",
							fieldtype: "Link",
							label: __("Public IPv4 Allocation"),
							options: "Public IP Allocation",
							reqd: 1,
							description: __(
								"The new virtual machine keeps the node name, key, and devices. Its devices reconnect after the node name moves to this address."
							),
							filters: {
								status: "Reserved",
								version: "4",
								tenant_id: 0,
								virtual_machine: ["is", "not set"],
							},
						},
					],
					primary_action_label: __("Rebuild"),
					primary_action(values) {
						frm.call({
							method: "rebuild",
							doc: frm.doc,
							args: values,
							freeze: true,
						}).then(() => {
							dialog.hide();
							frm.reload_doc();
						});
					},
				});
				dialog.show();
			},
			__("Dangerous Actions")
		);
	},
});
