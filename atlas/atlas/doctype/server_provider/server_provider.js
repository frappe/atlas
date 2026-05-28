const CREDENTIAL_CHECK_TTL_MS = 5 * 60 * 1000;


frappe.ui.form.on("Server Provider", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		if (frm.doc.is_active) {
			frappe.atlas.add_primary(frm, "Provision Server", () => open_provision_dialog(frm));
		}
		if (frm.doc.provider_type === "DigitalOcean") {
			frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
			render_credential_indicator(frm);
		}
		if (frm.doc.is_active) {
			frappe.atlas.add_danger(frm, "Archive", () => confirm_archive(frm));
		}
	},
});


function render_credential_indicator(frm) {
	const cache = frm._atlas_credential_cache;
	const fresh = cache && (Date.now() - cache.at) < CREDENTIAL_CHECK_TTL_MS;
	if (fresh) {
		paint_credential_indicator(frm, cache.result);
		return;
	}
	frm.call("credential_check").then(({message}) => {
		if (!message) return;
		frm._atlas_credential_cache = {at: Date.now(), result: message};
		paint_credential_indicator(frm, message);
	});
}


function paint_credential_indicator(frm, result) {
	if (!result || result.skipped) return;
	if (result.ok) {
		const remaining = result.rate_remaining != null
			? `${result.rate_remaining}/${result.rate_limit}`
			: __("rate-limit unknown");
		frm.dashboard.add_indicator(
			`✓ ${__("API token valid")} <span class="text-muted small">(${frappe.utils.escape_html(remaining)})</span>`,
			"green",
		);
	} else {
		frm.dashboard.add_indicator(
			`✗ ${__("API token invalid")} <span class="text-muted small">${frappe.utils.escape_html(result.error || "")}</span>`,
			"red",
		);
	}
}


function run_test_connection(frm) {
	frappe.show_alert({
		message: __("Testing connection…"),
		indicator: "blue",
	});
	frm.call("test_connection").then(({message}) => {
		frappe.show_alert({
			message: __("OK: {0}", [message.email]),
			indicator: "green",
		});
		frm._atlas_credential_cache = null;
		render_credential_indicator(frm);
	});
}


function open_provision_dialog(frm) {
	const is_self_managed = frm.doc.provider_type === "Self-Managed";
	const options_call = is_self_managed
		? Promise.resolve({message: {regions: [], sizes: [], images: []}})
		: frappe.call({method: "atlas.atlas.api.provider_options.provider_options"});

	options_call.then(({message: options}) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Provision Server"),
			fields: build_provision_fields(frm, options, is_self_managed),
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


function build_provision_fields(frm, options, is_self_managed) {
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
		fields.push(
			{
				fieldname: "region",
				label: __("Region"),
				fieldtype: "Select",
				options: options.regions.join("\n"),
				default: frm.doc.default_region,
				reqd: 1,
			},
			{
				fieldname: "size",
				label: __("Size"),
				fieldtype: "Select",
				options: options.sizes.join("\n"),
				default: frm.doc.default_size,
				reqd: 1,
			},
			{
				fieldname: "image",
				label: __("Image"),
				fieldtype: "Select",
				options: options.images.join("\n"),
				default: frm.doc.default_image,
				reqd: 1,
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
		: `<p>${__("This will create a {0} droplet in {1}.", [
			`<b>${frappe.utils.escape_html(values.size)}</b>`,
			`<b>${frappe.utils.escape_html(values.region)}</b>`,
		])}</p>`;

	frappe.atlas.confirm_cost({
		title: is_self_managed ? __("Bootstrap a self-managed server?") : __("Create a billable droplet?"),
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
	frappe.atlas.confirm_destructive({
		title: __("Archive {0}?", [frm.doc.provider_name]),
		body_html: "",
		match_string: frm.doc.provider_name,
		match_label: __("Type the provider name to confirm"),
		proceed_label: __("Archive"),
		proceed() {
			frm.call("archive").then(() => {
				frappe.show_alert({
					message: __("Provider archived."),
					indicator: "blue",
				});
				frm.reload_doc();
			});
		},
	});
}
