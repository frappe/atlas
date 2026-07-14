frappe.ui.form.on("Server", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		add_buttons(frm);
		render_capacity(frm);
	},
});

// Per-host capacity headline: axis fills plus the share-unit + stranded line the
// operator compares host shapes by (spec/24). Reads the same capacity_for_server
// helper placement uses; skips silently on an unmeasured/erroring host.
function render_capacity(frm) {
	frappe
		.call({
			method: "atlas.atlas.api.server_capacity.capacity_for_server",
			args: { server: frm.doc.name },
		})
		.then(({ message: capacity }) => {
			if (capacity) {
				frm.dashboard.set_headline(capacity_headline(capacity));
			}
		});
}

function capacity_headline(capacity) {
	const fill = (axis) =>
		axis.effective ? `${Math.round((100 * axis.used) / axis.effective)}%` : "—";
	let text = `Capacity — CPU ${fill(capacity.cpu)} · RAM ${fill(capacity.memory)} · disk ${fill(
		capacity.disk
	)}`;
	const units = capacity.share_units;
	if (units) {
		text += ` · ${units.free} / ${units.total} share units free`;
		const stranded = format_stranded(capacity.stranded);
		if (stranded) {
			text += ` · stranded: ${stranded}`;
		}
	}
	return `<span>${frappe.utils.escape_html(text)}</span>`;
}

function format_stranded(stranded) {
	if (!stranded) {
		return "";
	}
	const bits = [];
	if (stranded.cpu > 0.01) {
		bits.push(`${stranded.cpu.toFixed(1)} cores`);
	}
	if (stranded.memory > 0) {
		bits.push(`${Math.round(stranded.memory)} MB`);
	}
	if (stranded.disk > 0) {
		bits.push(`${Math.round(stranded.disk)} GB disk`);
	}
	return bits.join(", ");
}

function add_buttons(frm) {
	const status = frm.doc.status;
	if (status === "Archived") {
		return;
	}
	if (["Pending", "Bootstrapping", "Broken"].includes(status)) {
		frappe.atlas.add_primary(frm, "Bootstrap", () => confirm_bootstrap(frm));
		// Recover: re-drive a row whose finish_provisioning job was lost (stuck
		// Pending/Bootstrapping with a paid-for vendor box behind it). Unlike
		// Bootstrap, this runs the describe()-poll first to fill the NULL IPs.
		frappe.atlas.add_action(frm, "Recover", () => confirm_recover(frm));
	} else {
		frappe.atlas.add_success(frm, "Re-bootstrap", () => confirm_bootstrap(frm));
	}
	if (status === "Active") {
		frappe.atlas.add_action(frm, "Run Command", () => run_command(frm));
		frappe.atlas.add_action(frm, "Sync Image", () => open_sync_image_dialog(frm));
		frappe.atlas.add_action(frm, "Bake Image", () => open_bake_image_dialog(frm));
		frappe.atlas.add_action(frm, "Sync Scripts", () => sync_scripts(frm));
		frappe.atlas.add_action(frm, "Refresh Capacity", () => refresh_capacity(frm));
		frappe.atlas.add_action(frm, "Allocate Reserved IP", () =>
			confirm_allocate_reserved_ip(frm)
		);
		frappe.atlas.add_action(frm, "Discover Reserved IPs", () => discover_reserved_ips(frm));
	}
	frappe.atlas.add_action(frm, "Archive", () => confirm_archive(frm));
	frappe.atlas.add_danger(frm, "Reboot", () => confirm_reboot(frm));
}

// Stash this server as the SSH Console's one target and route there — a
// pre-targeted entry into the fleet-wide console, not a second execution path.
function run_command(frm) {
	window.localStorage.setItem(
		"ssh_console_prefill",
		JSON.stringify({ targets: [{ target_doctype: "Server", target_name: frm.doc.name }] })
	);
	frappe.set_route("Form", "SSH Console");
}

function confirm_allocate_reserved_ip(frm) {
	frappe.atlas.confirm_cost({
		title: __("Allocate a reserved IP for {0}?", [frm.doc.title]),
		body_html: `<p>${__(
			"Reserves a new public IPv4 at the provider (a billable resource) and adds it to this server's pool, unattached."
		)}</p>`,
		proceed_label: __("Allocate"),
		proceed() {
			frappe
				.call({
					method: "atlas.atlas.doctype.reserved_ip.reserved_ip.allocate",
					args: { server: frm.doc.name },
				})
				.then(({ message: name }) => {
					if (!name) return;
					frappe.show_alert({
						message: __("Reserved IP allocated."),
						indicator: "green",
					});
					frappe.set_route("Form", "Reserved IP", name);
				});
		},
	});
}

function discover_reserved_ips(frm) {
	// Read-only reconcile (vendor → Frappe): safe to run without a confirm.
	frappe
		.call({
			method: "atlas.atlas.doctype.reserved_ip.reserved_ip.discover",
			args: { server: frm.doc.name },
			freeze: true,
			freeze_message: __("Discovering reserved IPs…"),
		})
		.then(({ message: created }) => {
			const count = (created || []).length;
			frappe.show_alert(
				{
					message: count
						? __("Imported {0} reserved IP(s).", [count])
						: __("No new reserved IPs to import."),
					indicator: count ? "green" : "blue",
				},
				6
			);
			frm.dashboard.refresh();
		});
}

function sync_scripts(frm) {
	// Dev convenience: re-upload the durable atlas package + .py hooks to
	// /var/lib/atlas/bin without a full bootstrap. Pure code overwrite, no
	// vendor side effects, so no confirm.
	frm.call(
		"sync_scripts",
		{},
		{
			freeze: true,
			freeze_message: __("Syncing scripts…"),
		}
	).then(({ message: count }) => {
		frappe.show_alert(
			{
				message: __("Synced {0} script file(s) to {1}.", [count, frm.doc.title]),
				indicator: "green",
			},
			6
		);
	});
}

function refresh_capacity(frm) {
	// Re-measure the host's capacity facts (CPU/RAM/pool size + fullness) and stamp
	// them, without a full re-bootstrap. Read-only on the host, no vendor side
	// effects, so no confirm. Reloads the doc so the Capacity section updates.
	frm.call(
		"refresh_capacity_facts",
		{},
		{
			freeze: true,
			freeze_message: __("Measuring host capacity…"),
		}
	).then(() => {
		frm.reload_doc();
		frappe.show_alert(
			{
				message: __("Capacity facts refreshed for {0}.", [frm.doc.title]),
				indicator: "green",
			},
			6
		);
	});
}

function confirm_bootstrap(frm) {
	frappe.confirm(__("Bootstrap {0}?", [frm.doc.title]), () => {
		frm.call("bootstrap").then(({ message }) => {
			frappe.atlas.task_started(frm, "Bootstrap Server", message);
		});
	});
}

function confirm_recover(frm) {
	frappe.confirm(
		__(
			"Re-drive provisioning for {0}? Use this when the server is stuck Pending/Bootstrapping because its background job was lost — it re-runs the provider poll and bootstrap against the existing vendor resource (no re-provision).",
			[frm.doc.title]
		),
		() => {
			frm.call(
				"recover",
				{},
				{ freeze: true, freeze_message: __("Re-driving provisioning…") }
			).then(({ message: enqueued }) => {
				frappe.show_alert(
					{
						message: enqueued
							? __("Recovery job enqueued — the server will leave Pending shortly.")
							: __("A provisioning job is already in flight for this server."),
						indicator: enqueued ? "green" : "blue",
					},
					6
				);
			});
		}
	);
}

function confirm_reboot(frm) {
	frappe.atlas.confirm_destructive({
		title: __("Reboot {0}?", [frm.doc.title]),
		body_html: "",
		match_string: frm.doc.title,
		match_label: __("Type the server title to confirm"),
		proceed_label: __("Reboot"),
		proceed() {
			frm.call("reboot").then(({ message }) => {
				frappe.atlas.task_started(frm, "Reboot", message);
			});
		},
	});
}

function confirm_archive(frm) {
	frappe.atlas.confirm_archive(frm, {
		match: frm.doc.title,
		match_label: __("Type the server title to confirm"),
		alert_message: __("Server archived."),
		body_html: `<p>${__("Atlas will destroy the vendor resource. This is irreversible.")}</p>`,
	});
}

function open_sync_image_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Sync Image"),
		fields: [
			{
				fieldname: "image",
				label: __("Image"),
				fieldtype: "Link",
				options: "Virtual Machine Image",
				reqd: 1,
				only_select: 1,
				get_query: () => ({ filters: { is_active: 1 } }),
			},
		],
		primary_action_label: __("Sync"),
		primary_action(values) {
			frm.call("sync_image", { image: values.image }).then(({ message: task_name }) => {
				dialog.hide();
				frappe.atlas.task_started(frm, "Sync Image", task_name);
			});
		},
	});
	dialog.show();
}

// Bake a golden bench / proxy image: insert an Image Build row on this server.
// The row's after_insert enqueues the provision->build->snapshot job; we route
// to its form, whose live checklist shows the bake progress. Recipe choices mirror
// atlas.atlas.image_recipes.recipe_names() (the versioned bench variants + proxy);
// region is only relevant to the proxy recipe (which fixes a region).
function open_bake_image_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Bake Image"),
		fields: [
			{
				fieldname: "recipe",
				label: __("Recipe"),
				fieldtype: "Select",
				// Kept in lockstep with image_recipes.recipe_names() / the Image Build
				// `recipe` Select options. bench-v16 is the current line; the -admin
				// variants bake the bench-cli admin console (no site); the back-compat
				// `bench` alias is intentionally NOT an option (the operator picks an
				// explicit version, and a stored `bench` would fail the Select validation).
				options: [
					"bench-v16",
					"bench-v15",
					"bench-nightly",
					"bench-v16-admin",
					"bench-v15-admin",
					"bench-nightly-admin",
					"proxy",
				],
				default: "bench-v16",
				reqd: 1,
			},
			{
				fieldname: "region",
				label: __("Region"),
				fieldtype: "Data",
				depends_on: "eval:doc.recipe=='proxy'",
				mandatory_depends_on: "eval:doc.recipe=='proxy'",
				description: __("Required for the proxy recipe."),
			},
			{
				fieldname: "base_image",
				label: __("Base Image"),
				fieldtype: "Link",
				options: "Virtual Machine Image",
				only_select: 1,
				get_query: () => ({ filters: { is_active: 1 } }),
				description: __("Defaults to the active image if left blank."),
			},
			{
				fieldname: "terminate_build_vm",
				label: __("Terminate build VM after snapshot"),
				fieldtype: "Check",
				default: 0,
			},
		],
		primary_action_label: __("Bake"),
		primary_action(values) {
			frappe.db
				.insert({
					doctype: "Image Build",
					recipe: values.recipe,
					server: frm.doc.name,
					region: values.region || null,
					base_image: values.base_image || null,
					terminate_build_vm: values.terminate_build_vm ? 1 : 0,
				})
				.then((doc) => {
					dialog.hide();
					frappe.show_alert({ message: __("Bake started."), indicator: "blue" });
					frappe.set_route("Form", "Image Build", doc.name);
				});
		},
	});
	dialog.show();
}
