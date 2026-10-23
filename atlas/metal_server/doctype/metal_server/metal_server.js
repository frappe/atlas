// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Metal Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		const power_error = frm.doc.__onload?.bmc_power_error;
		frm.set_intro(
			power_error && __("BMC power state: {0}", [frappe.utils.escape_html(power_error)]),
			"red"
		);

		const is_deleted = frm.doc.status === "Deleted";
		const is_running = frm.doc.status === "Running";
		const is_stopped = frm.doc.status === "Stopped";
		const is_bmc_power_off = frm.doc.bmc_power_state === "Off";
		const server_label = `${frm.doc.title.bold()} (${frm.doc.name})`;
		const cpu_template_confirm_message = [
			frm.doc.is_cpu_template_disabled
				? __("Enable the CPU template on {0}?", [server_label])
				: __("Disable the CPU template on {0}?", [server_label]),
			[
				__("This runs Upgrade Metald, which restarts metald."),
				__("Running VMs keep their CPU configuration until their next cold boot."),
				__("Sleepy VMs lose their saved state and cold boot on their next start."),
			].join("<br>"),
			!frm.doc.is_cpu_template_disabled &&
				__(
					"<b>Risk:</b> a memory snapshot migration to or from this host falls back to disk-only, with a guest reboot, unless the other host also has the CPU template disabled and the same CPU model and features."
				),
		]
			.filter(Boolean)
			.join("<br><br>");

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
				__("Re-install Firewall"),
				"install_host_firewall",
				is_running,
				__("Installing firewall..."),
				false,
			],
			[
				__("Upgrade Metald"),
				"upgrade_metald",
				is_running,
				__("Upgrading Metald..."),
				__("Upgrade Metald on {0}?", [server_label]),
				true,
			],
			[
				frm.doc.is_cpu_template_disabled
					? __("Enable CPU Template")
					: __("Disable CPU Template"),
				"toggle_cpu_template",
				is_running,
				__("Upgrading Metald..."),
				cpu_template_confirm_message,
				true,
			],
			[
				__("Renew TLS Certificate"),
				"renew_tls_certificate",
				is_running && frm.doc.is_provisioning_completed,
				__("Renewing TLS certificate..."),
				__("Renew the TLS certificate on {0}? Metal restarts with it.", [server_label]),
				false,
			],
			[
				__("Configure Telemetry"),
				"configure_telemetry",
				is_running && frm.doc.is_provisioning_completed,
				__("Configuring telemetry..."),
				__("Install Vector and a new Datum token on {0}?", [server_label]),
			],
			[
				__("Reboot"),
				"reboot_server",
				!is_deleted && !is_stopped,
				__("Rebooting server..."),
				__("Reboot {0}?", [server_label]),
				true,
			],
			[
				__("Power Off"),
				"poweroff_server",
				!is_deleted && !is_stopped,
				__("Powering off server..."),
				__("Power off {0}?", [server_label]),
				true,
			],
			[
				__("Power On"),
				"poweron_server",
				!is_deleted && (is_stopped || is_bmc_power_off),
				__("Powering on server..."),
			],
			[
				__("Archive Server"),
				"archive_server",
				!is_deleted,
				__("Archiving server..."),
				__("Delete the provider server for {0}? The machine and its data are lost.", [
					server_label,
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

		if (is_running && frm.doc.__onload?.has_resizable_volumes) {
			["root", "storage"].forEach((kind) => {
				const title = __("Resize {0} Disk", [kind === "root" ? "Root" : "Storage"]);
				frm.add_custom_button(
					title,
					() => show_resize_volume_dialog(frm, kind, title),
					__("Actions")
				);
			});
		}

		if (is_running && frm.doc.is_provisioning_completed && !frm.doc.__onload?.running_drain) {
			// The Destinations field reads the child table's Link field from its metadata.
			frm.add_custom_button(
				__("Drain Server"),
				() =>
					frappe.model.with_doctype("Metal Server Drain Destination", () =>
						show_drain_dialog(frm)
					),
				__("Dangerous Actions")
			);
		}
	},
});

function show_drain_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Drain Metal Server"),
		fields: [
			{
				fieldtype: "Table MultiSelect",
				fieldname: "destinations",
				label: __("Destination Metal Servers"),
				options: "Metal Server Drain Destination",
				description: __(
					"<b>Leave empty</b> to let placement choose a Metal Server for each VM."
				),
				get_query: () => ({
					filters: {
						status: "Running",
						is_provisioning_completed: 1,
						name: ["!=", frm.doc.name],
					},
				}),
			},
			{
				fieldtype: "Check",
				fieldname: "has_memory_snapshot",
				label: __("Memory Snapshot"),
				default: 0,
			},
			{
				fieldtype: "Check",
				fieldname: "allow_disk_only_fallback",
				label: __("Allow Disk-Only Fallback"),
				description: __("Cold boots the guest if its memory cannot move."),
				default: 1,
				depends_on: "has_memory_snapshot",
			},
			{
				fieldtype: "Int",
				fieldname: "parallel_migrations",
				label: __("Parallel Migrations"),
				description: __("Migrations that run at the same time."),
				default: 1,
				reqd: 1,
			},
			{
				fieldtype: "Check",
				fieldname: "retry_failed_migrations",
				label: __("Retry Failed Migrations"),
				description: __(
					"Migrates a failed VM again after the other VMs, up to 3 attempts."
				),
				default: 1,
			},
		],
		primary_action_label: __("Drain"),
		primary_action(values) {
			dialog.hide();
			frm.call({
				method: "drain",
				doc: frm.doc,
				args: {
					destinations: (values.destinations || []).map((row) => row.metal_server),
					has_memory_snapshot: Boolean(values.has_memory_snapshot),
					allow_disk_only_fallback: Boolean(values.allow_disk_only_fallback),
					retry_failed_migrations: Boolean(values.retry_failed_migrations),
					parallel_migrations: values.parallel_migrations,
				},
				freeze: true,
				freeze_message: __("Starting drain..."),
			}).then(({ message: drain }) => frappe.set_route("Form", "Metal Server Drain", drain));
		},
	});
	dialog.show();
}

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
						"The provider allows one change to a volume every 6 hours."
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
						__("The disk grows on the host when the provider finishes the change.")
					);
				});
			},
		});
		dialog.show();
	});
}
