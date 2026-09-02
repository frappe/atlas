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
				true,
			],
			[__("Ping Server"), "ping_server", is_running, __("Pinging server..."), false],
			[__("Sync Disks"), "sync_disks", is_running, __("Syncing disks..."), false],
			[
				__("Re-configure WireGuard"),
				"configure_wireguard",
				is_running,
				__("Configuring WireGuard..."),
				false,
			],
			[
				__("Re-configure Metald"),
				"install_metald",
				is_running,
				__("Configuring Metald..."),
				false,
			],
			[
				__("Reboot"),
				"reboot_server",
				!is_deleted && !is_stopped,
				__("Rebooting server..."),
				__("Reboot {0}?", [frm.doc.name.bold()]),
				true,
			],
			[
				__("Power Off"),
				"poweroff_server",
				!is_deleted && !is_stopped,
				__("Powering off server..."),
				__("Power off {0}?", [frm.doc.name.bold()]),
				true,
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
				true,
			],
		].forEach(([label, method, condition, freeze_message, confirm_message, is_dangerous]) => {
			if (!condition) {
				return;
			}

			const call = () =>
				frm.call(method, { freeze: true, freeze_message }).then(() => frm.reload_doc());

			frm.add_custom_button(
				label,
				() => (confirm_message ? frappe.confirm(confirm_message, call) : call()),
				is_dangerous ? __("Dangerous Actions") : __("Actions")
			);
		});
	},
});
