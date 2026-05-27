frappe.ui.form.on("Server Provider", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frappe.atlas.add_primary(frm, "Provision Server", () => open_provision_dialog(frm));
		if (frm.doc.provider_type === "DigitalOcean") {
			frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
		}
	},
});


function run_test_connection(frm) {
	frm.call("test_connection").then(({message}) => {
		frappe.show_alert({
			message: __("OK: {0}", [message.email]),
			indicator: "green",
		});
	});
}


function open_provision_dialog(frm) {
	frm.call("preview_cost").then(({message: preview}) => {
		const is_self_managed = frm.doc.provider_type === "Self-Managed";
		const fields = build_provision_fields(preview, is_self_managed);
		const dialog = new frappe.ui.Dialog({
			title: __("Provision Server"),
			fields: fields,
			primary_action_label: __("Provision"),
			primary_action(values) {
				if (!validate_server_name(dialog, values.server_name)) return;
				dialog.hide();
				confirm_provision(frm, preview, values, is_self_managed);
			},
		});
		dialog.show();
	});
}


function build_provision_fields(preview, is_self_managed) {
	const fields = [
		{
			fieldname: "preview",
			fieldtype: "HTML",
			options: render_preview_html(preview, is_self_managed),
		},
		{
			fieldname: "server_name",
			label: __("Server Name"),
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
	}
	fields.push({
		fieldname: "footer_hint",
		fieldtype: "HTML",
		options: is_self_managed
			? `<div class="text-muted small">${__("Atlas will SSH to your host and run bootstrap-server.sh. Nothing is created remotely.")}</div>`
			: `<div class="text-muted small">${__("Provisioning takes ~90 seconds. The new Server form opens automatically and the bootstrap Task runs in the background.")}</div>`,
	});
	return fields;
}


function render_preview_html(preview, is_self_managed) {
	if (is_self_managed) {
		return `<div class="text-muted small">${__("Provider type: Self-Managed. You provide the IP addresses below.")}</div>`;
	}
	const cost = preview.monthly_cost_usd != null
		? `≈ $${preview.monthly_cost_usd}/mo`
		: "—";
	const rows = [
		[__("Region"), preview.region],
		[__("Size"), `${preview.size} <span class="text-muted">(${cost})</span>`],
		[__("Image"), preview.image],
	].map(([label, value]) => `
		<div class="row" style="padding: 4px 0">
			<div class="col-sm-4 text-muted">${frappe.utils.escape_html(label)}</div>
			<div class="col-sm-8">${value}</div>
		</div>
	`).join("");
	return `
		<div class="text-muted small mb-2">${__("Using defaults from {0}:", [frappe.utils.escape_html(preview.provider_type || "this provider")])}</div>
		${rows}
	`;
}


function validate_server_name(dialog, name) {
	if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(name)) {
		dialog.set_df_property(
			"server_name",
			"description",
			__("Lowercase + digits + hyphens, max 63 chars, must start with a letter or digit."),
		);
		frappe.show_alert({
			message: __("Server Name does not match the expected pattern."),
			indicator: "orange",
		});
		return false;
	}
	return true;
}


function confirm_provision(frm, preview, values, is_self_managed) {
	const body = is_self_managed
		? `<p>${__("Atlas will SSH to {0} as root and run bootstrap-server.sh. Nothing is created remotely.", [`<b>${frappe.utils.escape_html(values.ipv4_address)}</b>`])}</p>`
		: build_cost_body_html(preview);

	frappe.atlas.confirm_cost({
		title: is_self_managed ? __("Bootstrap a self-managed server?") : __("Create a billable droplet?"),
		body_html: body,
		proceed_label: __("Provision"),
		proceed() {
			frm.call("provision_server", values).then(({message: server_name}) => {
				frappe.show_alert({
					message: __("Provisioning {0}; watch the Task list.", [server_name]),
					indicator: "blue",
				});
				frappe.set_route("Form", "Server", server_name);
			});
		},
	});
}


function build_cost_body_html(preview) {
	const cost = preview.monthly_cost_usd != null
		? __("≈ ${0}/mo", [preview.monthly_cost_usd])
		: __("price not available");
	return `
		<p>${__("This will create a {0} droplet in {1} ({2}).", [
			`<b>${frappe.utils.escape_html(preview.size)}</b>`,
			`<b>${frappe.utils.escape_html(preview.region)}</b>`,
			cost,
		])}</p>
		<p>${__("It starts billing immediately and cannot be paused.")}</p>
	`;
}
