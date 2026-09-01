// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		const is_deleted = frm.doc.status === "Deleted";
		const is_running = frm.doc.status === "Running";
		const is_stopped = frm.doc.status === "Stopped";

		[
			[
				__("Retry Provisioning"),
				"setup_server",
				!frm.doc.is_provisioning_completed && !is_deleted,
				__("Starting server setup..."),
			],
			[__("Ping Server"), "ping_server", is_running, __("Pinging server...")],
			[
				__("Reboot"),
				"reboot_server",
				!is_deleted && !is_stopped,
				__("Rebooting server..."),
				__("Reboot {0}?", [frm.doc.name.bold()]),
			],
			[
				__("Power Off"),
				"poweroff_server",
				!is_deleted && !is_stopped,
				__("Powering off server..."),
				__("Power off {0}?", [frm.doc.name.bold()]),
			],
			[__("Power On"), "poweron_server", is_stopped, __("Powering on server...")],
			[
				__("Archive Server"),
				"archive_server",
				!is_deleted,
				__("Archiving server..."),
				__("Delete the provider server for {0}? The machine and its data are lost.", [
					frm.doc.name.bold(),
				]),
			],
		].forEach(([label, method, condition, freeze_message, confirm_message]) => {
			if (!condition) {
				return;
			}

			const call = () =>
				frm.call(method, { freeze: true, freeze_message }).then(() => frm.reload_doc());

			frm.add_custom_button(
				label,
				() => (confirm_message ? frappe.confirm(confirm_message, call) : call()),
				__("Actions")
			);
		});
	},
});
