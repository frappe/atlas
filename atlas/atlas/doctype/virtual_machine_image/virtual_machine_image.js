frappe.ui.form.on("Virtual Machine Image", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		if (frm.doc.is_active && frm.doc.rootfs_url) {
			frappe.atlas.add_primary(frm, "Sync to All Servers", () =>
				run_sync_to_all_servers(frm)
			);
		}
		// Export Image: a LOCAL (promoted-from-snapshot) image has no rootfs URL, so
		// Sync can't place it on another host — its bytes live on the one server it
		// was promoted on. Export ships them host-to-host over NBD (spec/08-images.md,
		// spec/24 §5.1). Only offered for a local image; a URL image uses Sync instead.
		if (frm.doc.is_active && !frm.doc.rootfs_url) {
			frappe.atlas.add_action(frm, "Export Image", () => open_export_dialog(frm));
			// Sync Across Hosts: the fleet-wide sibling of Export. Export ships this
			// local image to ONE host over NBD; this fans the same bytes out to MANY
			// over HTTP on the mesh, reusing the sync-image verb (no object store).
			frappe.atlas.add_action(frm, "Sync Across Hosts", () =>
				open_distribute_dialog(frm)
			);
		}
		if (frm.doc.is_active) {
			frappe.atlas.add_danger(frm, "Archive", () => confirm_archive(frm));
		}
	},
});

function open_export_dialog(frm) {
	const who = frm.doc.title || frm.doc.image_name;
	const dialog = new frappe.ui.Dialog({
		title: __("Export {0}", [who]),
		fields: [
			{
				fieldname: "target_server",
				label: __("Target Server"),
				fieldtype: "Link",
				options: "Server",
				reqd: 1,
				get_query: () => ({ filters: { status: "Active" } }),
				description: __(
					"Another Active host to copy this image to. Must share the source's provider."
				),
			},
			{
				fieldname: "cost_hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Copies the read-only base image LV and its kernel to the target over a plain-TCP NBD link, then registers it there. Runs phase by phase in the background; the target can provision from the image once it reaches Done."
				)}</p>`,
			},
		],
		primary_action_label: __("Export"),
		primary_action(values) {
			dialog.hide();
			frappe
				.call("atlas.atlas.export.export_image", {
					image: frm.doc.name,
					target_server: values.target_server,
				})
				.then(({ message: export_name }) => {
					if (!export_name) return;
					frappe.show_alert(
						{
							message: __("Export {0} started.", [export_name]),
							indicator: "blue",
						},
						6
					);
					frappe.set_route("Form", "Virtual Machine Image Export", export_name);
				});
		},
	});
	dialog.show();
}

function open_distribute_dialog(frm) {
	const who = frm.doc.title || frm.doc.image_name;
	const dialog = new frappe.ui.Dialog({
		title: __("Sync {0} across hosts", [who]),
		fields: [
			{
				fieldname: "servers",
				label: __("Target Hosts"),
				fieldtype: "MultiSelectList",
				get_data: (txt) =>
					frappe.db.get_link_options("Server", txt, { status: "Active" }),
				description: __(
					"Active hosts to copy this local image to over the mesh (no object store). The host it was promoted on is skipped automatically. Leave empty to sync to every other Active host."
				),
			},
			{
				fieldname: "how_hint",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Squashes the read-only base image LV on its home host, serves it over HTTP on the mesh, and runs the sync-image verb on each target so it builds an identical base LV. Runs in the background; watch the Task list."
				)}</p>`,
			},
		],
		primary_action_label: __("Sync"),
		primary_action(values) {
			dialog.hide();
			frappe
				.call("atlas.atlas.fleet_distribute.distribute_local_image", {
					image: frm.doc.name,
					servers: values.servers || [],
				})
				.then(({ message }) => {
					if (!message) return;
					frappe.show_alert(
						{
							message: __("Distributing {0} to {1} host(s); watch the Task list.", [
								frm.doc.name,
								(message.servers || []).length,
							]),
							indicator: "blue",
						},
						6
					);
				});
		},
	});
	// Default the picker to every Active host; distribute_local_image drops the source
	// host from the list server-side (it already holds the bytes).
	frappe.db
		.get_list("Server", { filters: { status: "Active" }, fields: ["name"], limit: 0 })
		.then((rows) =>
			dialog.set_value(
				"servers",
				(rows || []).map((row) => row.name)
			)
		);
	dialog.show();
}

function run_sync_to_all_servers(frm) {
	frappe.show_alert({ message: __("Syncing image to all servers…"), indicator: "blue" });
	frm.call("sync_to_all_servers").then(({ message: task_names }) => {
		const count = (task_names || []).length;
		frappe.show_alert({
			message: __("Syncing to {0} server(s); watch the Task list.", [count]),
			indicator: count ? "green" : "orange",
		});
	});
}

function confirm_archive(frm) {
	frappe.atlas.confirm_archive(frm, {
		match: frm.doc.title || frm.doc.image_name,
		match_label: __("Type the image title to confirm"),
		alert_message: __("Image archived."),
	});
}
