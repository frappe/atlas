frappe.ui.form.on("VPN Peer", {
	refresh(frm) {
		render_intro(frm);
		if (frm.is_new()) {
			return;
		}
		// Scope + crypto identity are frozen on the gateway once enrolled. Lock them in
		// the UI too so an existing peer reads as immutable.
		frm.set_df_property("tenant", "read_only", 1);
		frm.set_df_property("client_public_key", "read_only", 1);
		add_buttons(frm);
	},
});

function add_buttons(frm) {
	const status = frm.doc.status;
	if (status === "Active") {
		// Live. The customer needs the connection details (Atlas never had the
		// client's private key, so the config carries a placeholder).
		frappe.atlas.add_primary(frm, "Show client config", () => show_client_config(frm));
		// Re-enroll re-runs the gateway reconcile + mesh push, e.g. after a gateway
		// rebuild left the peer off wg0.
		frappe.atlas.add_success(frm, "Re-enroll", () => re_enroll(frm));
		frappe.atlas.add_danger(frm, "Revoke", () => confirm_revoke(frm));
	} else if (status === "Pending") {
		// Only sits here if enrollment failed at Save. Retry or abandon.
		frappe.atlas.add_primary(frm, "Re-enroll", () => re_enroll(frm));
		frappe.atlas.add_danger(frm, "Revoke", () => confirm_revoke(frm));
	}
	// Revoked: terminal, no actions — only the intro.
}

function render_intro(frm) {
	frm.set_intro("");
	if (frm.is_new()) {
		frm.set_intro(
			__(
				"On your machine run <code>wg genkey | tee privatekey | wg pubkey > publickey</code>, paste the <b>public</b> key below, pick your Tenant, label the device, and Save. Atlas enrolls your laptop on the gateway automatically — then <b>Show client config</b> gives you the <code>.conf</code>. Your private key never leaves your machine."
			),
			"blue"
		);
		return;
	}
	const status = frm.doc.status;
	if (status === "Pending") {
		frm.set_intro(
			__("Enrollment did not complete on the gateway. Click Re-enroll to retry."),
			"orange"
		);
	} else if (status === "Active") {
		frm.set_intro(
			__(
				"This peer is live on the gateway. Click Show client config for the connection details and setup steps, then <code>wg-quick up</code> on your machine."
			),
			"green"
		);
	} else if (status === "Revoked") {
		frm.set_intro(
			__(
				"This peer was revoked and dropped from the gateway. Create a new peer to reconnect."
			),
			"red"
		);
	}
}

function re_enroll(frm) {
	frm.call("re_enroll").then(() => {
		frappe.show_alert({ message: __("Peer re-enrolled on the gateway"), indicator: "green" });
		frm.reload_doc();
	});
}

function show_client_config(frm) {
	frm.call("client_config").then(({ message }) => {
		if (!message) {
			return;
		}
		open_config_dialog(frm, message);
	});
}

function open_config_dialog(frm, cfg) {
	const dialog = new frappe.ui.Dialog({
		title: __("WireGuard client config"),
		size: "large",
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"This tunnel reaches every VM in your <b>{0}</b> VPC by its <code>fdaa:</code> address, and nothing else — not other tenants, not the internet. Save the block below as <code>/etc/wireguard/tenant-vpc.conf</code> and replace <code>&lt;your client private key&gt;</code> with the contents of your <code>privatekey</code> file — Atlas never sees it.",
					[frappe.utils.escape_html(frm.doc.tenant)]
				)}</p>`,
			},
			{
				fieldname: "config",
				fieldtype: "Code",
				label: __("tenant-vpc.conf"),
				options: "Properties",
				read_only: 1,
			},
			{
				fieldname: "instructions",
				fieldtype: "HTML",
				options: `<div class="text-muted small" style="margin-top: var(--margin-sm)"><b>${__(
					"Setup"
				)}</b><pre style="white-space: pre-wrap; margin-top: var(--margin-xs)">${frappe.utils.escape_html(
					cfg.instructions
				)}</pre></div>`,
			},
		],
		primary_action_label: __("Copy config"),
		primary_action() {
			frappe.utils.copy_to_clipboard(cfg.config);
		},
	});
	dialog.show();
	// Set the Code field after show so the ACE editor is mounted.
	dialog.set_value("config", cfg.config);
	return dialog;
}

function confirm_revoke(frm) {
	frappe.atlas.confirm_destructive({
		title: __("Revoke this VPC peer?"),
		body_html: `<p>${__(
			"Drops the peer from the gateway and withdraws its address from the mesh. The client loses access to the whole VPC immediately. This cannot be undone — create a new peer to reconnect."
		)}</p>`,
		match_string: frm.doc.label,
		match_label: __("Type the peer label to confirm"),
		proceed_label: __("Revoke"),
		proceed() {
			frm.call("revoke").then(() => {
				frappe.show_alert({
					message: __("Peer revoked"),
					indicator: "red",
				});
				frm.reload_doc();
			});
		},
	});
}
