// Atlas Setup Wizard slides — the human front-end of the explicit setup contract
// (atlas/setup.py). Registered into the Frappe Setup Wizard via the
// `setup_wizard_requires` hook. The slide field VALUES are posted to
// `atlas.setup.get_setup_stages` (the `setup_wizard_stages` hook), whose stage
// fns call the Layer-1 `setup()` setters.
//
// Hide-irrelevant-fields is pure `depends_on`. Per-provider required fields use
// `mandatory_depends_on` so Next-validation doesn't block on a hidden field.
//
// The imperative bits (in each slide's `onload`) deliver the "discover, don't
// type" experience: a Test Connection button calls `atlas.setup.wizard_discover`
// with the just-typed (unsaved) credentials and turns the Size / Image / SSH Key /
// Project slug boxes into pick-lists populated from the vendor's live catalog. The
// default provider is applied on load so its section shows immediately (no blank
// first paint), and the SSH-key slide validates the path before Next.

frappe.provide("frappe.setup");

frappe.setup.on("before_load", function () {
	atlas_setup_slides().forEach((slide) => frappe.setup.add_slide(slide));
});

// Fields the wizard discovers rather than asks you to type. Start as empty Selects
// (so they render as dropdowns, not text); Test Connection fills the options.
const ATLAS_DISCOVERED = {
	DigitalOcean: ["do_default_size", "do_default_image", "do_ssh_key_id"],
	Scaleway: ["scw_default_size", "scw_default_image", "scw_project_id", "scw_ssh_key_id"],
};

function atlas_setup_slides() {
	return [
		{
			name: "atlas_provider",
			title: __("Provider"),
			icon: "fa fa-server",
			fields: [
				{
					fieldname: "provider_type",
					label: __("Provider"),
					fieldtype: "Select",
					options: ["DigitalOcean", "Scaleway", "Self-Managed"].join("\n"),
					default: "DigitalOcean",
					reqd: 1,
				},
				{
					fieldname: "region",
					label: __("Atlas Region"),
					fieldtype: "Data",
					reqd: 1,
					description: __(
						"This Atlas's single region (the source of truth, e.g. blr1). NOT the same as the provider's own API region/zone below — a provider operates in many regions."
					),
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
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __(
						"The DO API region Atlas provisions droplets in, e.g. blr1 (the vendor's own region)."
					),
				},
				{
					fieldname: "do_ssh_key_id",
					label: __("SSH Key (optional)"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __(
						"Pick after Test Connection, or leave blank — Atlas will find or upload the SSH public key automatically."
					),
				},
				{
					fieldname: "do_default_size",
					label: __("Default Size"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __(
						"Pick after Test Connection, or type a vendor slug, e.g. s-2vcpu-4gb-intel."
					),
				},
				{
					fieldname: "do_default_image",
					label: __("Default Image"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __(
						"Pick after Test Connection, or type a vendor slug, e.g. ubuntu-24-04-x64."
					),
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
						"Pick after Test Connection (the catalog needs it to list SSH keys)."
					),
				},
				{
					fieldname: "scw_default_size",
					label: __("Default Size"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __(
						"Pick after Test Connection, or type a case-sensitive offer name, e.g. EM-A610R-NVME."
					),
				},
				{
					fieldname: "scw_default_image",
					label: __("Default Image"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __(
						"Pick after Test Connection, or type a case-sensitive OS slug, e.g. Ubuntu_24.04."
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
					fieldname: "scw_ssh_key_id",
					label: __("IAM SSH Key (optional)"),
					fieldtype: "Select",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __(
						"Pick after Test Connection, or leave blank to let Atlas register the SSH public key with IAM at provision time."
					),
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
			],

			onload: atlas_provider_slide_onload,
		},

		{
			name: "atlas_ssh_key",
			title: __("SSH Key"),
			icon: "fa fa-key",
			fields: [
				{
					fieldname: "ssh_private_key_path",
					label: __("SSH Private Key Path"),
					fieldtype: "Data",
					reqd: 1,
					description: __(
						"Absolute path on the controller (0600, readable by the Frappe user). The public key is derived from it via ssh-keygen if you leave the next field blank."
					),
				},
				{
					fieldname: "ssh_public_key",
					label: __("SSH Public Key (optional)"),
					fieldtype: "Small Text",
					description: __(
						"OpenSSH public key body. Derived from the private key path when omitted."
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
					description: __(
						"e.g. blr1.frappe.dev (its Route 53 hosted zone must already exist)."
					),
				},
				{
					fieldname: "route53_access_key_id",
					label: __("Route 53 Access Key ID"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls",
					mandatory_depends_on: "eval:doc.setup_tls",
				},
				{
					fieldname: "route53_secret_access_key",
					label: __("Route 53 Secret Access Key"),
					fieldtype: "Password",
					depends_on: "eval:doc.setup_tls",
					mandatory_depends_on: "eval:doc.setup_tls",
				},
				{
					fieldname: "route53_region",
					label: __("AWS API Region"),
					fieldtype: "Data",
					default: "us-east-1",
					depends_on: "eval:doc.setup_tls",
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
	// Apply the default provider so its section renders on first paint (Frappe leaves
	// the doc value empty even when the Select visually shows the first option).
	if (!slide.get_value("provider_type")) {
		slide.get_field("provider_type").set_input("DigitalOcean");
		slide.form.refresh();
	}

	slide
		.get_field("do_test_connection")
		?.$input?.on("click", () => atlas_test_connection(slide, "DigitalOcean"));
	slide
		.get_field("scw_test_connection")
		?.$input?.on("click", () => atlas_test_connection(slide, "Scaleway"));

	// Scaleway lists SSH keys only once a project is chosen — re-fetch on change.
	slide.get_field("scw_project_id")?.$input?.on("change", () => {
		if (slide.get_value("scw_project_id"))
			atlas_test_connection(slide, "Scaleway", { silent: true });
	});
}

function atlas_test_connection(slide, provider_type, opts = {}) {
	const credentials =
		provider_type === "DigitalOcean"
			? { api_token: slide.get_value("do_api_token") }
			: {
					secret_key: slide.get_value("scw_secret_key"),
					zone: slide.get_value("scw_zone"),
					organization_id: slide.get_value("scw_organization_id"),
					project_id: slide.get_value("scw_project_id"),
					billing: slide.get_value("scw_billing"),
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
// operator already picked/typed.
function atlas_apply_catalog(slide, provider_type, catalog) {
	const map =
		provider_type === "DigitalOcean"
			? {
					do_default_size: catalog.sizes,
					do_default_image: catalog.images,
					do_ssh_key_id: catalog.ssh_keys,
			  }
			: {
					scw_default_size: catalog.sizes,
					scw_default_image: catalog.images,
					scw_project_id: catalog.projects,
					scw_ssh_key_id: catalog.ssh_keys,
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
