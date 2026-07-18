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
		frappe.atlas.add_action(frm, "Discover Reserved IPs", () =>
			open_discover_reserved_ips_dialog(frm)
		);
		frappe.atlas.add_action(frm, "Bake Golden Image", () => confirm_bake(frm));
		frappe.atlas.add_action(frm, "Ensure Proxy", () => confirm_ensure_proxy(frm));

		// Fake provider only: populate a clickable demo fleet (catalog, images,
		// servers, VMs + snapshots) so the lifecycle is explorable with no real cloud.
		if (frm.doc.provider_type === "Fake") {
			frappe.atlas.add_action(frm, "Generate Demo Data", () => open_demo_data_dialog(frm));
		}
	},
});

// Generate Demo Data (Fake provider only). Kicks off the background job that
// stands up the varied demo fleet (atlas.atlas.demo.run) — faked, so no real cloud.
function open_demo_data_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Generate demo data"),
		fields: [
			{
				fieldname: "intro",
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"Stands up a realistic, varied fleet on the Fake provider — Servers across " +
						"every status, Virtual Machines in every state, snapshots, Reserved IPs, " +
						"and back-dated Tasks. No real cloud, no SSH. Idempotent: a plain run " +
						"reuses existing demo rows."
				)}</p>`,
			},
			{
				fieldname: "reset",
				label: __("Reset (wipe the Fake fleet and rebuild)"),
				fieldtype: "Check",
				default: 0,
				description: __(
					"Deletes the existing demo Servers / VMs / snapshots / IPs first. Real DigitalOcean / Scaleway rows are never touched."
				),
			},
		],
		primary_action_label: __("Generate"),
		primary_action(values) {
			dialog.hide();
			frm.call("generate_demo_data", { reset: values.reset ? 1 : 0 }).then(() => {
				frappe.show_alert({
					message: __("Generating demo data; watch the Server / Virtual Machine lists."),
					indicator: "blue",
				});
			});
		},
	});
	dialog.show();
}

// The desk equivalents of bootstrap's `bake_golden_image` / `ensure_proxy` steps:
// each acts on the newest Active Server and is billable + multi-minute, so the
// controller enqueues a `long` background job — these buttons only kick it off.

function confirm_bake(frm) {
	frappe.atlas.confirm_cost({
		title: __("Bake the golden bench image?"),
		body_html: `<p>${__(
			"Provisions a build VM, builds bench inside it, snapshots it, and wires it as " +
				"the default_bench_snapshot self-serve site VMs clone from. Billable + slow " +
				"(several minutes). Reuses an Available snapshot if one is already configured."
		)}</p>`,
		proceed_label: __("Bake"),
		proceed() {
			frm.call("bake_golden_image").then(({ message: server_name }) => {
				frappe.show_alert({
					message: __("Baking golden image on {0}; watch the Task list.", [server_name]),
					indicator: "blue",
				});
			});
		},
	});
}

function confirm_ensure_proxy(frm) {
	frappe.atlas.confirm_cost({
		title: __("Stand up the proxy VM?"),
		body_html: `<p>${__(
			"Provisions a proxy VM, builds the nginx+Lua stack inside it, and attaches a " +
				"reserved IPv4 — the public front door subdomains route through. Billable " +
				"(one VM + one reserved IPv4). Reuses a Running proxy in the region if present."
		)}</p>`,
		proceed_label: __("Stand up proxy"),
		proceed() {
			frm.call("ensure_proxy").then(({ message: server_name }) => {
				frappe.show_alert({
					message: __("Standing up the proxy on {0}; watch the Task list.", [
						server_name,
					]),
					indicator: "blue",
				});
			});
		},
	});
}

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
	// Prefill the Size / Image picks from the Provider Size / Provider Image rows
	// marked `is_default` for this provider_type (the same default provision_server
	// falls back to). Self-Managed and Fake have no catalog default — resolve to null.
	const defaults_promise = is_self_managed
		? Promise.resolve([null, null])
		: Promise.all([
				default_catalog_row("Provider Size", frm.doc.provider_type),
				default_catalog_row("Provider Image", frm.doc.provider_type),
		  ]);
	defaults_promise.then(([default_size, default_image]) => {
		const dialog = new frappe.ui.Dialog({
			title: __("Provision Server"),
			fields: build_provision_fields(frm, { default_size, default_image }, is_self_managed),
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

// The `name` of the default catalog row (e.g. "DigitalOcean/s-2vcpu-4gb"), or null.
function default_catalog_row(doctype, provider_type) {
	return frappe.db
		.get_value(doctype, { provider_type, is_default: 1, enabled: 1 }, "name")
		.then(({ message }) => message?.name || null);
}

function build_provision_fields(frm, defaults, is_self_managed) {
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
				default: defaults.default_size,
				reqd: 1,
				get_query: () => ({ filters: link_filters }),
			},
			{
				fieldname: "image",
				label: __("Image"),
				fieldtype: "Link",
				options: "Provider Image",
				default: defaults.default_image,
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

// Discover Reserved IPs — the recovery path after the Reserved IP rows are gone
// (a server reset dropped them). The vendor still holds the IPs; each one's droplet
// binding maps it back to a Server (auto-resolved server-side). Mirrors Discover
// Servers: read-only listing → tick → import.
function open_discover_reserved_ips_dialog(frm) {
	frappe.show_alert({ message: __("Discovering reserved IPs…"), indicator: "blue" });
	frm.call("discover_reserved_ips").then(({ message: ips }) => {
		if (!ips || !ips.length) {
			frappe.msgprint({
				title: __("No reserved IPs found"),
				message: __("The vendor account holds no reserved IPs in this region/zone."),
				indicator: "orange",
			});
			return;
		}
		render_discover_reserved_ips_dialog(frm, ips);
	});
}

function render_discover_reserved_ips_dialog(frm, ips) {
	const dialog = new frappe.ui.Dialog({
		title: __("Reserved IPs at {0}", [frm.doc.provider_type]),
		size: "large",
		fields: [
			{
				fieldname: "ips_html",
				fieldtype: "HTML",
				options: discover_reserved_ips_table_html(ips),
			},
			{
				fieldname: "caveat",
				fieldtype: "HTML",
				options: `<div class="text-muted small" style="margin-top: 8px;">${__(
					"Each IP is mapped to its Server automatically by its droplet binding. " +
						"A floating IP (attached to no droplet, or one Atlas doesn't model) " +
						"imports unattached — reassign it to a Server later."
				)}</div>`,
			},
		],
		primary_action_label: __("Import selected"),
		primary_action() {
			const addrs = checked_reserved_ip_addresses(dialog);
			if (!addrs.length) {
				frappe.show_alert({
					message: __("Tick at least one reserved IP to import."),
					indicator: "orange",
				});
				return;
			}
			dialog.hide();
			run_import_reserved_ips(frm, addrs);
		},
	});
	dialog.show();
}

function discover_reserved_ips_table_html(ips) {
	const rows = ips
		.map((ip) => {
			const addr = frappe.utils.escape_html(ip.ip_address);
			const server = ip.server
				? frappe.utils.escape_html(ip.server)
				: `<span class="text-muted">${__("floating")}</span>`;
			// Already-modeled IPs render disabled + badged so a re-run can't
			// double-insert — the same dedup discipline as Discover Servers.
			const badge = ip.imported
				? `<span class="indicator-pill gray">${__("imported")}</span>`
				: "";
			const checkbox = `<input type="checkbox" class="atlas-discover-rip" data-ip-address="${addr}" ${
				ip.imported ? "disabled" : ""
			}>`;
			const dim = ip.imported ? ' style="opacity: 0.5;"' : "";
			return `<tr${dim}>
				<td style="width: 32px; text-align: center;">${checkbox}</td>
				<td>${addr}</td>
				<td>${server}</td>
				<td>${badge}</td>
			</tr>`;
		})
		.join("");
	return `<table class="table table-bordered" style="margin-bottom: 0;">
		<thead>
			<tr>
				<th style="width: 32px;"></th>
				<th>${__("IPv4")}</th>
				<th>${__("Server")}</th>
				<th></th>
			</tr>
		</thead>
		<tbody>${rows}</tbody>
	</table>`;
}

function checked_reserved_ip_addresses(dialog) {
	const addrs = [];
	dialog.$wrapper.find("input.atlas-discover-rip:checked").each(function () {
		addrs.push($(this).data("ip-address").toString());
	});
	return addrs;
}

function run_import_reserved_ips(frm, ip_addresses) {
	frappe.show_alert({
		message: __("Importing {0} reserved IP(s)…", [ip_addresses.length]),
		indicator: "blue",
	});
	// The dialog posts ip_addresses as a JSON string; import_reserved_ips parses it.
	frm.call("import_reserved_ips", { ip_addresses: JSON.stringify(ip_addresses) }).then(
		({ message }) => {
			const imported = (message && message.imported) || [];
			if (!imported.length) {
				frappe.show_alert({
					message: __("Nothing imported (already modeled)."),
					indicator: "orange",
				});
				return;
			}
			const summary = imported
				.map((row) => `${row.ip_address} → ${row.server || __("floating")}`)
				.join(", ");
			frappe.msgprint({
				title: __("Imported {0} reserved IP(s)", [imported.length]),
				message: frappe.utils.escape_html(summary),
				indicator: "green",
			});
			frappe.set_route("List", "Reserved IP");
		}
	);
}
