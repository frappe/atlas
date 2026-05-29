frappe.ui.form.on("Provider", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		if (frm.doc.is_active) {
			frappe.atlas.add_primary(frm, "Provision Server", () => open_provision_dialog(frm));
		}
		frappe.atlas.add_action(frm, "Authenticate", () => run_authenticate(frm));
		frappe.atlas.add_action(frm, "Refresh Catalog", () => run_refresh_catalog(frm));
		if (frm.doc.is_active) {
			frappe.atlas.add_danger(frm, "Archive", () => confirm_archive(frm));
		}
	},
});


function run_authenticate(frm) {
	frappe.show_alert({message: __("Authenticating…"), indicator: "blue"});
	frm.call("authenticate").then(({message}) => {
		if (message.ok) {
			const label = message.account_label || frm.doc.provider_name;
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
	frappe.show_alert({message: __("Refreshing catalog…"), indicator: "blue"});
	frm.call("discover_and_upsert").then(({message}) => {
		const summary = __("Catalog refreshed: {0} inserted, {1} updated, {2} disabled",
			[message.inserted, message.updated, message.disabled]);
		frappe.show_alert({message: summary, indicator: "green"});
	});
}


function open_provision_dialog(frm) {
	const is_self_managed = frm.doc.provider_type === "Self-Managed";
	frappe.db.get_doc(`${frm.doc.provider_type} Settings`).then((settings) => {
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
			},
		);
	} else {
		const link_filters = {provider_type: frm.doc.provider_type, enabled: 1};
		fields.push(
			{
				fieldname: "size",
				label: __("Size"),
				fieldtype: "Link",
				options: "Provider Size",
				default: settings ? settings.default_size : null,
				reqd: 1,
				get_query: () => ({filters: link_filters}),
			},
			{
				fieldname: "image",
				label: __("Image"),
				fieldtype: "Link",
				options: "Provider Image",
				default: settings ? settings.default_image : null,
				reqd: 1,
				get_query: () => ({filters: link_filters}),
			},
		);
	}
	return fields;
}


function validate_server_title(dialog, title) {
	if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(title)) {
		dialog.set_df_property(
			"title",
			"description",
			__("Lowercase + digits + hyphens, max 63 chars, must start with a letter or digit."),
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
		? `<p>${__("Atlas will SSH to {0} as root and run bootstrap-server.sh. Nothing is created remotely.", [`<b>${frappe.utils.escape_html(values.ipv4_address)}</b>`])}</p>`
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
			frm.call("provision_server", values).then(({message: server_name}) => {
				frappe.show_alert({
					message: __("Provisioning {0}; watch the Task list.", [values.title]),
					indicator: "blue",
				});
				frappe.set_route("Form", "Server", server_name);
			});
		},
	});
}


function confirm_archive(frm) {
	frappe.atlas.confirm_archive(frm, {
		match: frm.doc.provider_name,
		match_label: __("Type the provider name to confirm"),
		alert_message: __("Provider archived."),
	});
}
