// Atlas Setup Wizard slides — the human front-end of the explicit setup contract
// (atlas/setup.py). Registered into the Frappe Setup Wizard via the
// `setup_wizard_requires` hook. The slide field VALUES are posted to
// `atlas.setup.get_setup_stages` (the `setup_wizard_stages` hook), whose stage
// fns call the Layer-1 `setup()` setters.
//
// Hide-irrelevant-fields is pure `depends_on` (no imperative JS). Per-provider
// required fields use `mandatory_depends_on` so Next-validation doesn't block on a
// hidden field. The only imperative bits are the optional "Test Connection / Fetch
// Catalog" buttons, which call the EXISTING whitelisted methods.

frappe.provide("frappe.setup");

frappe.setup.on("before_load", function () {
	atlas_setup_slides().forEach((slide) => frappe.setup.add_slide(slide));
});

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
					label: __("SSH Key ID"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __("DO's numeric key id or its SHA-256 fingerprint."),
				},
				{
					fieldname: "do_default_size",
					label: __("Default Size"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __("Vendor-native slug, e.g. s-2vcpu-4gb-intel."),
				},
				{
					fieldname: "do_default_image",
					label: __("Default Image"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='DigitalOcean'",
					mandatory_depends_on: "eval:doc.provider_type=='DigitalOcean'",
					description: __("Vendor-native slug, e.g. ubuntu-24-04-x64."),
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
					fieldname: "scw_project_id",
					label: __("Project ID"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
				},
				{
					fieldname: "scw_zone",
					label: __("Zone"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __(
						"The Scaleway Elastic Metal zone, e.g. fr-par-2 (the vendor's own zone)."
					),
				},
				{
					fieldname: "scw_default_size",
					label: __("Default Size"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __("Case-sensitive offer name, e.g. EM-A610R-NVME."),
				},
				{
					fieldname: "scw_default_image",
					label: __("Default Image"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					mandatory_depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __("Case-sensitive OS slug, e.g. Ubuntu_24.04."),
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
					label: __("IAM SSH Key UUID (optional)"),
					fieldtype: "Data",
					depends_on: "eval:doc.provider_type=='Scaleway'",
					description: __(
						"Leave blank to let Atlas register the SSH public key with IAM at provision time."
					),
				},

				// --- Self-Managed (per-server networking → provision payload, not a Single) ---
				{
					fieldtype: "Section Break",
					label: __("Self-Managed Networking"),
					depends_on: "eval:doc.provider_type=='Self-Managed'",
				},
				{
					fieldname: "self_managed_note",
					fieldtype: "HTML",
					options: `<p class="text-muted">${__(
						"These are per-server networking values for the existing box. They are forwarded to Provision Server, not stored as global config — the wizard records them for the bootstrap step."
					)}</p>`,
					depends_on: "eval:doc.provider_type=='Self-Managed'",
				},
			],
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
					fieldname: "acme_directory_url",
					label: __("ACME Directory URL"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_tls",
					description: __(
						"Defaults to Let's Encrypt STAGING (untrusted, no rate limits). Set the production URL for a trusted cert."
					),
				},
			],
		},

		{
			name: "atlas_email",
			title: __("Email"),
			icon: "fa fa-envelope",
			fields: [
				{
					fieldname: "setup_email",
					label: __("Configure outbound email"),
					fieldtype: "Check",
					default: 0,
				},
				{ fieldtype: "Section Break", depends_on: "eval:doc.setup_email" },
				{
					fieldname: "smtp_host",
					label: __("SMTP Host"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_email",
					mandatory_depends_on: "eval:doc.setup_email",
				},
				{
					fieldname: "smtp_port",
					label: __("SMTP Port"),
					fieldtype: "Int",
					default: 587,
					depends_on: "eval:doc.setup_email",
				},
				{
					fieldname: "smtp_login",
					label: __("SMTP Login"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_email",
					mandatory_depends_on: "eval:doc.setup_email",
				},
				{
					fieldname: "smtp_password",
					label: __("SMTP Password"),
					fieldtype: "Password",
					depends_on: "eval:doc.setup_email",
					mandatory_depends_on: "eval:doc.setup_email",
				},
				{
					fieldname: "smtp_from",
					label: __("From Address (optional)"),
					fieldtype: "Data",
					depends_on: "eval:doc.setup_email",
					description: __("Defaults to the SMTP login."),
				},
			],
		},

		{
			name: "atlas_golden_snapshot",
			title: __("Golden Snapshot"),
			icon: "fa fa-camera",
			fields: [
				{
					fieldname: "default_bench_snapshot",
					label: __("Default Bench Snapshot (optional)"),
					fieldtype: "Link",
					options: "Virtual Machine Snapshot",
					get_query: () => ({ filters: { status: "Available" } }),
					description: __(
						"The golden bench image self-serve site VMs clone from. Leave blank if you haven't baked one yet."
					),
				},
			],
		},
	];
}
