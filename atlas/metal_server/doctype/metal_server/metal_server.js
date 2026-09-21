// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metal Server", {
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
				__("Sync Host State"),
				"sync_state",
				is_running && frm.doc.is_provisioning_completed,
				__("Syncing host state..."),
				false,
			],
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
				__("Upgrade Metald"),
				"upgrade_metald",
				is_running,
				__("Upgrading Metald..."),
				__("Upgrade Metald on {0}?", [frm.doc.name.bold()]),
				true,
			],
			[
				__("Renew TLS Certificate"),
				"renew_tls_certificate",
				is_running && frm.doc.is_provisioning_completed,
				__("Renewing TLS certificate..."),
				__("Renew the TLS certificate on {0}? Metal restarts with it.", [
					frm.doc.name.bold(),
				]),
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

			const call = () => {
				frm.call({ method, doc: frm.doc, freeze: true, freeze_message }).then(() => {
					if (["sync_disks", "sync_state"].includes(method)) {
						frappe.msgprint(
							__(
								"Synchronization is queued. This page will refresh in a few seconds."
							)
						);
						setTimeout(() => frm.reload_doc(), 5_000);
						return;
					}

					frm.reload_doc();
				});
			};

			frm.add_custom_button(
				label,
				() => (confirm_message ? frappe.confirm(confirm_message, call) : call()),
				is_dangerous ? __("Dangerous Actions") : __("Actions")
			);
		});

		if (is_running && frm.doc.__onload?.server_provider === "AWS") {
			["root", "storage"].forEach((kind) => {
				const title = __("Resize {0} Disk", [kind === "root" ? "Root" : "Storage"]);
				frm.add_custom_button(
					title,
					() => show_resize_volume_dialog(frm, kind, title),
					__("Actions")
				);
			});
		}
	},
});

function show_resize_volume_dialog(frm, kind, title) {
	frm.call({
		method: "get_volume",
		doc: frm.doc,
		args: { kind },
		freeze: true,
		freeze_message: __("Loading disk details..."),
	}).then(({ message: volume }) => {
		const field = (fieldname, label, fieldtype = "Int") => ({
			fieldname,
			label,
			fieldtype,
			read_only: fieldtype === "Data",
			default: volume[fieldname],
		});
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [
				{
					fieldtype: "HTML",
					options: `<p class="text-danger">${__(
						"AWS allows one change to a volume every 6 hours."
					)}</p>`,
				},
				{ fieldtype: "Section Break" },
				field("volume_type", __("Type"), "Data"),
				field("last_modified_at", __("Last Changed At"), "Data"),
				field("modification_state", __("Last Change State"), "Data"),
				{ fieldtype: "Column Break" },
				field("size_gib", __("Size (GiB)")),
				field("iops", __("IOPS")),
				field("throughput_mibps", __("Throughput (MiB/s)")),
			],
			primary_action_label: __("Resize"),
			primary_action({ size_gib, iops, throughput_mibps }) {
				frm.call({
					method: "resize_volume",
					doc: frm.doc,
					args: { kind, size_gib, iops, throughput_mibps },
					freeze: true,
					freeze_message: __("Resizing disk..."),
				}).then(() => {
					dialog.hide();
					frappe.show_alert(
						__("The disk grows on the host when AWS finishes the change.")
					);
				});
			},
		});
		dialog.show();
	});
}
