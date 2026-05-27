frappe.ui.form.on("Virtual Machine Image", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frappe.atlas.add_secondary(frm, "Sync to Server", () => open_sync_to_server_dialog(frm));
		frappe.atlas.add_action(frm, "Sync to All Servers", () => confirm_sync_to_all(frm));
	},
});


function open_sync_to_server_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Sync to Server"),
		fields: [
			{
				fieldname: "server_name",
				label: __("Server"),
				fieldtype: "Link",
				options: "Server",
				only_select: 1,
				reqd: 1,
				get_query: () => ({filters: {status: "Active"}}),
			},
			{
				fieldname: "hint",
				fieldtype: "HTML",
				options: `<div class="text-muted small">${__("Each download takes a few minutes per server depending on image size.")}</div>`,
			},
		],
		primary_action_label: __("Sync"),
		primary_action(values) {
			frm.call("sync_to_server", {server_name: values.server_name})
				.then(({message: task_name}) => {
					dialog.hide();
					frappe.atlas.task_started(frm, "Sync image", task_name);
				});
		},
	});
	dialog.show();
}


function confirm_sync_to_all(frm) {
	frappe.db.get_list("Server", {
		fields: ["name", "region", "status"],
		filters: {status: "Active"},
		order_by: "name asc",
		limit: 100,
	}).then((servers) => {
		if (!servers.length) {
			frappe.show_alert({
				message: __("No active servers to sync to."),
				indicator: "orange",
			});
			return;
		}
		const target_rows = servers.map((server) => `
			<li><b>${frappe.utils.escape_html(server.name)}</b>
				<span class="text-muted">${frappe.utils.escape_html(server.region || "")} · ${frappe.utils.escape_html(server.status)}</span>
			</li>
		`).join("");
		const body = `
			<p>${__("Image: {0}", [`<b>${frappe.utils.escape_html(frm.doc.image_name || frm.doc.name)}</b>`])}</p>
			<p>${__("Targets:")}</p>
			<ul class="list-unstyled" style="padding-left: 1em">${target_rows}</ul>
			<p class="text-muted small">${__("Each download fetches kernel + rootfs over the public internet, verifies SHA-256, and runs sync-image.sh.")}</p>
		`;
		frappe.atlas.confirm_cost({
			title: __("Sync to {0} active server(s)?", [servers.length]),
			body_html: body,
			proceed_label: __("Sync to All"),
			proceed() {
				frm.call("sync_to_all_servers").then(({message}) => {
					frappe.show_alert({
						message: __("Enqueued {0} sync Task(s).", [message.length]),
						indicator: "blue",
					});
				});
			},
		});
	});
}
