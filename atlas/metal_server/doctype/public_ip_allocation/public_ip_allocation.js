frappe.ui.form.on("Public IP Allocation", {
	async refresh(frm) {
		if (frm.doc.failure_message) frm.set_intro(frm.doc.failure_message, "red");
		if (frm.is_new()) return;

		const { message } = await frappe.db.get_value("Public IP Pool", frm.doc.pool, "gateway");
		const isRouted = Boolean(message?.gateway);
		const actions = [
			[
				__("Attach to Virtual Machine"),
				() => showAttachDialog(frm),
				frm.doc.status === "Reserved" && !frm.doc.virtual_machine && !isRouted,
			],
			[
				__("Detach from Virtual Machine"),
				() => detachFromVirtualMachine(frm),
				["Attaching", "Attached"].includes(frm.doc.status) && frm.doc.virtual_machine,
			],
			[
				__("Reserve"),
				() => reservePublicIP(frm),
				frm.doc.status === "Attached" && !frm.doc.is_reserved && !isRouted,
			],
			[
				__("Release Reservation"),
				() => releaseReservation(frm),
				frm.doc.status === "Reserved" && !frm.doc.virtual_machine,
			],
			[
				__("Retry Operation"),
				() => frm.call("retry_operation"),
				["Attaching", "Detaching"].includes(frm.doc.status),
			],
			[__("Block"), () => blockPublicIP(frm), frm.doc.status === "Available"],
			[
				__("Unblock"),
				() => frm.call("unblock").then(() => frm.reload_doc()),
				frm.doc.status === "Blocked",
			],
		];

		actions.forEach(([label, action, condition]) => {
			if (condition) frm.add_custom_button(label, action, __("Actions"));
		});
	},
});

function showAttachDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Attach Public IP"),
		fields: [
			{
				fieldname: "virtual_machine",
				fieldtype: "Link",
				label: __("Virtual Machine"),
				options: "Virtual Machine",
				reqd: 1,
				get_query: () => ({
					filters: { tenant_id: frm.doc.tenant_id, is_draft: 0, is_terminating: 0 },
				}),
			},
		],
		primary_action_label: __("Attach"),
		primary_action(values) {
			frm.call("attach_to_virtual_machine", values).then(() => {
				dialog.hide();
				frm.reload_doc();
			});
		},
	});
	dialog.show();
}

function detachFromVirtualMachine(frm) {
	frappe.confirm(
		__("Detach {0} from {1}?", [frm.doc.prefix.bold(), frm.doc.virtual_machine.bold()]),
		() => frm.call("detach_from_virtual_machine").then(() => frm.reload_doc())
	);
}

function reservePublicIP(frm) {
	frappe.confirm(
		__("Keep {0} for tenant {1} after detach?", [frm.doc.prefix.bold(), frm.doc.tenant_id]),
		() => frm.call("reserve").then(() => frm.reload_doc())
	);
}

function releaseReservation(frm) {
	frappe.confirm(__("Release {0} to its pool?", [frm.doc.prefix.bold()]), () =>
		frm.call("release_reservation").then(() => frm.reload_doc())
	);
}

function blockPublicIP(frm) {
	frappe.confirm(
		__("Block {0}? The pool will not allocate it until you unblock it.", [
			frm.doc.prefix.bold(),
		]),
		() => frm.call("block").then(() => frm.reload_doc())
	);
}
