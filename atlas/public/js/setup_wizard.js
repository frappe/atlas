// Atlas Setup Wizard slides — the human front-end of the explicit setup contract
// (atlas/setup.py). Registered into the Frappe Setup Wizard via the
// `setup_wizard_requires` hook. The slide field VALUES are posted to
// `atlas.setup.get_setup_stages` (the `setup_wizard_stages` hook), whose stage
// fns call the Layer-1 `setup()` setters.
//
// The slides are ordered controller-first, then cloud, then optional TLS:
//
//   1. Atlas  — this installation's OWN facts: its single region (the source of
//               truth) and the controller SSH private key. Both vendor-independent,
//               so they come before any provider talk and need no "not the same as
//               the provider region" disclaimer.
//   2. Provider — purely how Atlas reaches the cloud: provider type + per-vendor
//               credentials / region / project.
//   3. TLS    — optional wildcard certificate.
//
// Hide-irrelevant-fields is pure `depends_on`. Per-provider required fields use
// `mandatory_depends_on` so Next-validation doesn't block on a hidden field.
//
// The imperative bits (in each slide's `onload`) deliver the "discover, don't
// type" experience: a Test Connection button calls `atlas.setup.wizard_discover`
// with the just-typed (unsaved) credentials — plus the controller key path from
// the Atlas slide — and turns the Project slug box into a pick-list from the
// vendor's live catalog. It ALSO find-or-registers the controller key with the
// vendor and stashes the resulting vendor SSH key id (`matched_ssh_key_id`) into a
// hidden field the stage posts — so the operator never picks or uploads an SSH key
// by hand. The default provider is applied on load so its section shows immediately
// (no blank first paint).
//
// The default size/image are NOT asked here — `setup()` adopts the provider's
// discover() default into the empty catalog slot (operator's `atlas_*_default_*`
// config keys override it), and the operator can flip the default on the Provider
// Size / Provider Image list anytime.

frappe.provide("frappe.setup");

frappe.setup.on("before_load", function () {
	atlas_setup_slides().forEach((slide) => frappe.setup.add_slide(slide));
});

// Selects the wizard discovers rather than asks you to type. Start empty (so they
// render as dropdowns, not text); Test Connection fills the options. The vendor SSH
// key is NOT here — it is resolved silently into a hidden field, not picked.
const ATLAS_DISCOVERED = {
	DigitalOcean: [],
	Scaleway: ["scw_project_id"],
};

function atlas_setup_slides() {
	return [
		{
			name: "atlas",
			title: __("Atlas"),
			icon: "fa fa-globe",
			fields: [
				{
					fieldname: "region",
					label: __("Atlas Region"),
					fieldtype: "Data",
					reqd: 1,
					description: __(
						"This Atlas's single region — the source of truth, e.g. blr1. (Distinct from the provider's own API region/zone, set on the next slide.)"
					),
				},
				{
					fieldname: "ssh_private_key_path",
					label: __("SSH Private Key Path"),
					fieldtype: "Data",
					reqd: 1,
					description: __(
						"Absolute path on the controller (0600, readable by the Frappe user) Atlas uses to reach the boxes it provisions. The matching public key is derived via ssh-keygen and registered with your provider for you when you Test Connection."
					),
				},
				{
					fieldname: "ssh_key_hint",
					fieldtype: "HTML",
					options: `<p class="text-muted small">${__(
						"The file only needs to exist on the controller before you provision a Server — setup saves the path either way."
					)}</p>`,
				},
			],
		},

		{
			name: "atlas_provider",
			title: __("Provider"),
			icon: "fa fa-server",
			fields: [
				{
					fieldname: "provider_type",
					label: __("Provider"),
					fieldtype: "Select",
					options: ["DigitalOcean", "Scaleway", "Self-Managed", "Fake"].join("\n"),
					default: "DigitalOcean",
					reqd: 1,
				},

				// --- DigitalOcean ---
				{
					fieldtype: "Section Break",
					label: __("DigitalOcean"),
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
				},
				{
					fieldname: "do_api_token",
					label: __("API Token"),
					fieldtype: "Password",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
				},
				{
					fieldname: "do_test_connection",
					fieldtype: "Button",
					label: __("Test Connection & Fetch Catalog"),
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
				},
				{
					fieldname: "do_connection_status",
					fieldtype: "HTML",
					options: "",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
				},
				{
					fieldname: "do_region",
					label: __("DigitalOcean Region"),
					fieldtype: "Data",
					default: "blr1",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __(
						"The DO API region Atlas provisions droplets in, e.g. blr1 (the vendor's own region)."
					),
				},
				{
					// Hidden: Test Connection find-or-registers the controller key and
					// writes the resolved DO key id here for the stage to post.
					fieldname: "do_ssh_key_id",
					fieldtype: "Data",
					hidden: 1,
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
				},

				// --- Scaleway ---
				{
					fieldtype: "Section Break",
					label: __("Scaleway"),
					depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					fieldname: "scw_secret_key",
					label: __("Secret Key"),
					fieldtype: "Password",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					fieldname: "scw_zone",
					label: __("Zone"),
					fieldtype: "Select",
					options: ["", "fr-par-1", "fr-par-2", "nl-ams-1", "pl-waw-2"].join("\n"),
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __("The Scaleway Elastic Metal zone (the vendor's own zone)."),
				},
				{
					fieldname: "scw_test_connection",
					fieldtype: "Button",
					label: __("Test Connection & Fetch Catalog"),
					depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					fieldname: "scw_connection_status",
					fieldtype: "HTML",
					options: "",
					depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					fieldname: "scw_project_id",
					label: __("Project"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __(
						"Pick after Test Connection (the SSH key is registered into this project)."
					),
				},
				{ fieldtype: "Column Break", depends_on: "eval:doc.provider_type=='Scaleway'" },
				{
					fieldname: "scw_organization_id",
					label: __("Organization ID (optional)"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					fieldname: "scw_billing",
					label: __("Billing"),
					fieldtype: "Select",
					options: ["hourly", "monthly"].join("\n"),
					default: "hourly",
					depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					// Hidden: Test Connection find-or-registers the controller key with
					// IAM and writes the resolved key id here for the stage to post.
					fieldname: "scw_ssh_key_id",
					fieldtype: "Data",
					hidden: 1,
					depends_on: "eval:doc.provider_type=='Scaleway'",
				},

				// --- Self-Managed ---
				{
					fieldtype: "Section Break",
					label: __("Self-Managed"),
					depends_on: "eval:doc.provider_type=='Self-Managed'",
				},
				{
					fieldname: "self_managed_note",
					fieldtype: "HTML",
					options: `<p class="text-muted">${__(
						"Self-Managed has no global vendor config. Per-server networking is entered when you provision the box (Provision Server), not here."
					)}</p>`,
					depends_on: "eval:doc.provider_type=='Self-Managed'",
				},

				// --- Fake ---
				{
					fieldtype: "Section Break",
					label: __("Fake"),
					depends_on: "eval:doc.provider_type=='Fake'",
				},
				{
					fieldname: "fake_note",
					fieldtype: "HTML",
					options: `<p class="text-muted">${__(
						"The Fake provider is a development-only, no-op vendor — no credentials, no real cloud, no SSH. It needs developer_mode enabled."
					)}</p>`,
					depends_on: "eval:doc.provider_type=='Fake'",
				},
				{
					fieldname: "fake_generate_demo_data",
					label: __("Generate demo data after setup"),
					fieldtype: "Check",
					default: 1,
					depends_on: "eval:doc.provider_type=='Fake'",
					description: __(
						"Stands up a varied Fake fleet — Servers across every status, Virtual Machines in every state, snapshots, Reserved IPs, and back-dated Tasks — so the desk is populated the moment setup finishes. You can also re-run it anytime from Generate Demo Data on Atlas Settings."
					),
				},
			],

			onload: atlas_provider_slide_onload,
		},

		{
			name: "atlas_tls",
			title: __("TLS"),
			icon: "fa fa-lock",
			fields: [
				{
					fieldname: "setup_tls",
					label: __("Configure TLS / wildcard certificate"),
					fieldtype: "Check",
					default: 0,
				},
				{ fieldtype: "Section Break", depends_on: "eval:doc.setup_tls" },
				{
					fieldname: "tls_domain",
					label: __("Wildcard Domain"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls",
					mandatory_depends_on: "eval:doc.setup_tls",
					description: __("e.g. blr1.frappe.dev (the DNS zone must already exist)."),
				},
				{
					fieldname: "dns_provider_type",
					label: __("DNS Provider"),
					fieldtype: "Select",
					options: ["Route53", "PowerDNS"].join("\n"),
					default: "Route53",
					depends_on: "eval:doc.setup_tls",
					mandatory_depends_on: "eval:doc.setup_tls",
				},
				{
					fieldname: "route53_access_key_id",
					label: __("Route 53 Access Key ID"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='Route53'",
					mandatory_depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='Route53'",
				},
				{
					fieldname: "route53_secret_access_key",
					label: __("Route 53 Secret Access Key"),
					fieldtype: "Password",
					depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='Route53'",
					mandatory_depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='Route53'",
				},
				{
					fieldname: "route53_region",
					label: __("AWS API Region"),
					fieldtype: "Data",
					default: "us-east-1",
					depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='Route53'",
				},
				{
					fieldname: "powerdns_api_url",
					label: __("PowerDNS API URL"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='PowerDNS'",
					mandatory_depends_on:
						"eval:doc.setup_tls && doc.dns_provider_type=='PowerDNS'",
					description: __("Base URL without /api/v1."),
				},
				{
					fieldname: "powerdns_api_key",
					label: __("PowerDNS API Key"),
					fieldtype: "Password",
					depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='PowerDNS'",
					mandatory_depends_on:
						"eval:doc.setup_tls && doc.dns_provider_type=='PowerDNS'",
				},
				{
					fieldname: "powerdns_server_id",
					label: __("PowerDNS Server ID"),
					fieldtype: "Data",
					default: "localhost",
					depends_on: "eval:doc.setup_tls && doc.dns_provider_type=='PowerDNS'",
				},
				{
					fieldname: "acme_account_email",
					label: __("ACME Account Email"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls",
					mandatory_depends_on: "eval:doc.setup_tls",
				},
				{
					fieldname: "acme_environment",
					label: __("Certificate Environment"),
					fieldtype: "Select",
					options: [
						"Staging (untrusted, no rate limits)",
						"Production (trusted)",
						"Custom URL",
					].join("\n"),
					default: "Staging (untrusted, no rate limits)",
					depends_on: "eval:doc.setup_tls",
					description: __(
						"Staging issues untrusted test certs with no rate limits. Switch to Production for a real, browser-trusted certificate."
					),
				},
				{
					fieldname: "acme_directory_url",
					label: __("ACME Directory URL"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls && doc.acme_environment=='Custom URL'",
					mandatory_depends_on: "eval:doc.acme_environment=='Custom URL'",
				},
			],
		},
	];
}

// --- Provider slide: default-on-load + Test Connection / auto-fill -----------

function atlas_provider_slide_onload(slide) {
	// Seed declared `default`s into the doc on first paint. The slide FieldGroup shows
	// the first/declared option visually but leaves the doc value empty, so a field a
	// user never touches would post blank and fail its `mandatory_depends_on` — make
	// "what you see selected" == "what gets posted".
	["provider_type", "do_region", "dns_provider_type", "powerdns_server_id"].forEach(
		(fieldname) => {
			const field = slide.get_field(fieldname);
			if (field?.df.default && !slide.get_value(fieldname))
				field.set_input(field.df.default);
		}
	);
	slide.form.refresh();

	slide
		.get_field("do_test_connection")
		?.$input?.on("click", () => atlas_test_connection(slide, "DigitalOcean"));
	slide
		.get_field("scw_test_connection")
		?.$input?.on("click", () => atlas_test_connection(slide, "Scaleway"));

	// Scaleway resolves the SSH key (and lists keys) only once a project is chosen —
	// re-probe on change to register the controller key into the picked project.
	slide.get_field("scw_project_id")?.$input?.on("change", () => {
		if (slide.get_value("scw_project_id"))
			atlas_test_connection(slide, "Scaleway", { silent: true });
	});
}

function atlas_test_connection(slide, provider_type, opts = {}) {
	// The controller key path lives on the Atlas slide (visited before this one); it is
	// already merged into the wizard's accumulated values. Pass it so the probe can
	// find-or-register the matching vendor SSH key.
	const ssh_private_key_path = frappe.wizard?.values?.ssh_private_key_path || "";
	const credentials =
		provider_type === "DigitalOcean"
			? { api_token: slide.get_value("do_api_token"), ssh_private_key_path }
			: {
					secret_key: slide.get_value("scw_secret_key"),
					zone: slide.get_value("scw_zone"),
					organization_id: slide.get_value("scw_organization_id"),
					project_id: slide.get_value("scw_project_id"),
					billing: slide.get_value("scw_billing"),
					ssh_private_key_path,
			  };

	const status_field =
		provider_type === "DigitalOcean" ? "do_connection_status" : "scw_connection_status";
	if (!opts.silent) atlas_set_status(slide, status_field, "blue", __("Testing connection…"));

	frappe.call({
		method: "atlas.setup.wizard_discover",
		args: { provider_type, credentials },
		callback: ({ message }) => {
			if (!message) return;
			if (message.ok) {
				atlas_apply_catalog(slide, provider_type, message);
				atlas_set_status(
					slide,
					status_field,
					"green",
					__("Connected: {0}", [
						frappe.utils.escape_html(message.account_label || provider_type),
					])
				);
			} else if (!opts.silent) {
				atlas_set_status(
					slide,
					status_field,
					"red",
					frappe.utils.escape_html(message.error || __("Connection failed."))
				);
			}
		},
		error: () => {
			if (!opts.silent)
				atlas_set_status(slide, status_field, "red", __("Connection failed."));
		},
	});
}

// Turn the discovered lists into <select> options, preserving any value the
// operator already picked/typed, and stash the resolved vendor SSH key id into its
// hidden field (the probe find-or-registered the controller key for us).
function atlas_apply_catalog(slide, provider_type, catalog) {
	const map =
		provider_type === "DigitalOcean"
			? {}
			: {
					scw_project_id: catalog.projects,
			  };

	for (const [fieldname, items] of Object.entries(map)) {
		if (!items || !items.length) continue;
		atlas_fill_select(
			slide,
			fieldname,
			items,
			ATLAS_DISCOVERED[provider_type]?.includes(fieldname)
		);
	}

	const ssh_key_field = provider_type === "DigitalOcean" ? "do_ssh_key_id" : "scw_ssh_key_id";
	if (catalog.matched_ssh_key_id)
		slide.get_field(ssh_key_field)?.set_value(catalog.matched_ssh_key_id);
}

function atlas_fill_select(slide, fieldname, items, optional) {
	const field = slide.get_field(fieldname);
	if (!field) return;
	const current = slide.get_value(fieldname);
	// `value` is what the setter stores (vendor slug / id); `label` is what we show.
	const seen = new Set();
	const options = [];
	if (optional) options.push({ value: "", label: "" });
	for (const item of items) {
		if (seen.has(item.value)) continue;
		seen.add(item.value);
		options.push({ value: item.value, label: item.label || item.value });
	}
	field.df.options = options;
	field.refresh();
	if (current && seen.has(current)) field.set_input(current);
}

function atlas_set_status(slide, fieldname, color, html) {
	const field = slide.get_field(fieldname);
	if (!field) return;
	field.html(
		`<div class="text-${
			color === "green" ? "success" : color === "red" ? "danger" : "muted"
		}" style="margin:4px 0;">${html}</div>`
	);
}
