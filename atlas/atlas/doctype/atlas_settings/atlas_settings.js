// Atlas Settings — Single. Holds the active provider_type and the provider
// buttons the deleted Provider DocType used to own. The whitelisted methods
// (provision_server / authenticate / refresh_catalog / discover_servers /
// import_servers) live on the Atlas Settings controller.

frappe.ui.form.on("Atlas Settings", {
	refresh(frm) {
		if (!frm.doc.provider_type) {
			return;
		}
		frappe.atlas.add_primary(frm, "Provision Server", () => open_provision_dialog(frm));
		frappe.atlas.add_action(frm, "Authenticate", () => run_authenticate(frm));
		frappe.atlas.add_action(frm, "Refresh Catalog", () => run_refresh_catalog(frm));
		frappe.atlas.add_action(frm, "Discover Servers", () => open_discover_servers_dialog(frm));
	},
});

function run_authenticate(frm) {
	frappe.show_alert({ message: __("Authenticating…"), indicator: "blue" });
	frm.call("authenticate").then(({ message }) => {
		if (message.ok) {
			const label = message.account_label || frm.doc.provider_type;
			frappe.show_alert({
				message: __("OK: {0}", [label]),
				indicator: "green",
			});
		} else {
			frappe.show_alert({
				message: __("Failed: {0}", [message.error || __("unknown error")]),
				indicator: "red",
			});
		}
	});
}

function run_refresh_catalog(frm) {
	frappe.show_alert({ message: __("Refreshing catalog…"), indicator: "blue" });
	frm.call("refresh_catalog").then(({ message }) => {
		const summary = __("Catalog refreshed: {0} inserted, {1} updated, {2} disabled", [
			message.inserted,
			message.updated,
			message.disabled,
		]);
		frappe.show_alert({ message: summary, indicator: "green" });
	});
}

function open_provision_dialog(frm) {
	const is_self_managed = frm.doc.provider_type === "Self-Managed";
	const settings_doctype = `${frm.doc.provider_type} Settings`;
	// The Fake provider (developer_mode) has no Settings Single; default to no
	// settings so the dialog still renders. DO/Scaleway/Self-Managed always have one.
	const settings_promise = frappe.db.exists("DocType", settings_doctype)
		? frappe.db.get_doc(settings_doctype)
		: Promise.resolve(null);
	settings_promise.then((settings) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Provision Server"),
			fields: build_provision_fields(frm, settings, is_self_managed),
			primary_action_label: __("Provision"),
			primary_action(values) {
				if (!validate_server_title(dialog, values.title)) return;
				dialog.hide();
				confirm_provision(frm, values, is_self_managed);
			},
		});
		dialog.show();
	});
}

function build_provision_fields(frm, settings, is_self_managed) {
	const fields = [
		{
			fieldname: "title",
			label: __("Title"),
			fieldtype: "Data",
			reqd: 1,
			description: __("lowercase + digits + hyphens, max 63 chars"),
		},
	];
	if (is_self_managed) {
		fields.push(
			{
				fieldname: "ipv4_address",
				label: __("IPv4 Address"),
				fieldtype: "Data",
				reqd: 1,
				description: __("Public IPv4 Atlas will SSH to."),
			},
			{
				fieldname: "ipv6_address",
				label: __("IPv6 Address"),
				fieldtype: "Data",
				reqd: 1,
				description: __("The host's own IPv6."),
			},
			{
				fieldname: "ipv6_prefix",
				label: __("IPv6 Prefix"),
				fieldtype: "Data",
				reqd: 1,
				description: __("Full prefix routed to the host, e.g. 2a03:b0c0:abcd:1234::/64."),
			},
			{
				fieldname: "ipv6_virtual_machine_range",
				label: __("IPv6 Virtual Machine Range"),
				fieldtype: "Data",
				reqd: 1,
				description: __("Subnet Atlas allocates VM addresses from. Any prefix length."),
			}
		);
	} else {
		const link_filters = { provider_type: frm.doc.provider_type, enabled: 1 };
		fields.push(
			{
				fieldname: "size",
				label: __("Size"),
				fieldtype: "Link",
				options: "Provider Size",
				default: settings ? settings.default_size : null,
				reqd: 1,
				get_query: () => ({ filters: link_filters }),
			},
			{
				fieldname: "image",
				label: __("Image"),
				fieldtype: "Link",
				options: "Provider Image",
				default: settings ? settings.default_image : null,
				reqd: 1,
				get_query: () => ({ filters: link_filters }),
			}
		);
	}
	return fields;
}

function validate_server_title(dialog, title) {
	if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(title)) {
		dialog.set_df_property(
			"title",
			"description",
			__("Lowercase + digits + hyphens, max 63 chars, must start with a letter or digit.")
		);
		frappe.show_alert({
			message: __("Title does not match the expected pattern."),
			indicator: "orange",
		});
		return false;
	}
	return true;
}

function confirm_provision(frm, values, is_self_managed) {
	const body = is_self_managed
		? `<p>${__(
				"Atlas will SSH to {0} as root and run bootstrap-server.sh. Nothing is created remotely.",
				[`<b>${frappe.utils.escape_html(values.ipv4_address)}</b>`]
		  )}</p>`
		: `<p>${__("This will create a {0} server (~90 s to bootstrap).", [
				`<b>${frappe.utils.escape_html(values.size)}</b>`,
		  ])}</p>`;

	frappe.atlas.confirm_cost({
		title: is_self_managed
			? __("Bootstrap a self-managed server?")
			: __("Create a billable server?"),
		body_html: body,
		proceed_label: __("Provision"),
		proceed() {
			frm.call("provision_server", values).then(({ message: server_name }) => {
				frappe.show_alert({
					message: __("Provisioning {0}; watch the Task list.", [values.title]),
					indicator: "blue",
				});
				frappe.set_route("Form", "Server", server_name);
			});
		},
	});
}

function open_discover_servers_dialog(frm) {
	frappe.show_alert({ message: __("Discovering servers…"), indicator: "blue" });
	frm.call("discover_servers").then(({ message: servers }) => {
		if (!servers || !servers.length) {
			frappe.msgprint({
				title: __("No servers found"),
				message: __("The vendor account holds no servers in this region/zone."),
				indicator: "orange",
			});
			return;
		}
		render_discover_dialog(frm, servers);
	});
}

function render_discover_dialog(frm, servers) {
	const dialog = new frappe.ui.Dialog({
		title: __("Servers at {0}", [frm.doc.provider_type]),
		size: "large",
		fields: [
			{
				fieldname: "servers_html",
				fieldtype: "HTML",
				options: discover_table_html(servers),
			},
			{
				fieldname: "caveat",
				fieldtype: "HTML",
				options: `<div class="text-muted small" style="margin-top: 8px;">${__(
					"Imported servers land as Pending. A box built outside Atlas must be " +
						"Bootstrapped before it can host VMs, and may not match Atlas's " +
						"RAID-1 / LVM-pool layout — Bootstrap can fail on disk discovery if it doesn't."
				)}</div>`,
			},
		],
		primary_action_label: __("Import selected"),
		primary_action() {
			const ids = checked_resource_ids(dialog);
			if (!ids.length) {
				frappe.show_alert({
					message: __("Tick at least one server to import."),
					indicator: "orange",
				});
				return;
			}
			dialog.hide();
			run_import_servers(frm, ids);
		},
	});
	dialog.show();
}

function discover_table_html(servers) {
	const rows = servers
		.map((server) => {
			const id = frappe.utils.escape_html(server.provider_resource_id);
			const title = frappe.utils.escape_html(server.title || server.provider_resource_id);
			const ipv4 = frappe.utils.escape_html(server.ipv4_address || "—");
			const size = frappe.utils.escape_html(server.size || "—");
			// Already-modeled servers render disabled + badged so a re-run can't
			// double-insert — the same dedup discipline as Reserved IP.discover(),
			// surfaced in the picker.
			const badge = server.imported
				? `<span class="indicator-pill gray">${__("imported")}</span>`
				: "";
			const checkbox = `<input type="checkbox" class="atlas-discover-pick" data-resource-id="${id}" ${
				server.imported ? "disabled" : ""
			}>`;
			const dim = server.imported ? ' style="opacity: 0.5;"' : "";
			return `<tr${dim}>
				<td style="width: 32px; text-align: center;">${checkbox}</td>
				<td>${title}</td>
				<td>${ipv4}</td>
				<td>${size}</td>
				<td>${badge}</td>
			</tr>`;
		})
		.join("");
	return `<table class="table table-bordered" style="margin-bottom: 0;">
		<thead>
			<tr>
				<th style="width: 32px;"></th>
				<th>${__("Title")}</th>
				<th>${__("IPv4")}</th>
				<th>${__("Size")}</th>
				<th></th>
			</tr>
		</thead>
		<tbody>${rows}</tbody>
	</table>`;
}

function checked_resource_ids(dialog) {
	const ids = [];
	dialog.$wrapper.find("input.atlas-discover-pick:checked").each(function () {
		ids.push($(this).data("resource-id").toString());
	});
	return ids;
}

function run_import_servers(frm, resource_ids) {
	frappe.show_alert({
		message: __("Importing {0} server(s)…", [resource_ids.length]),
		indicator: "blue",
	});
	// The dialog posts resource_ids as a JSON string; import_servers parses it.
	frm.call("import_servers", { resource_ids: JSON.stringify(resource_ids) }).then(
		({ message }) => {
			const imported = (message && message.imported) || [];
			if (!imported.length) {
				frappe.show_alert({
					message: __("Nothing imported (already modeled)."),
					indicator: "orange",
				});
				return;
			}
			const names = imported.map((row) => row.title).join(", ");
			frappe.msgprint({
				title: __("Imported {0} server(s)", [imported.length]),
				message: __("Imported: {0}. Open each and click Bootstrap to bring it Active.", [
					frappe.utils.escape_html(names),
				]),
				indicator: "green",
			});
			// Route to the first imported Server form so the operator can Bootstrap it.
			frappe.set_route("Form", "Server", imported[0].name);
		}
	);
}
