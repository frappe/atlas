frappe.ui.form.on("VPN Tunnel", {
	refresh(frm) {
		render_intro(frm);
		if (frm.is_new()) {
			return;
		}
		// Identity is frozen on the host after bring-up (validate() enforces it).
		// Lock it in the UI too so an existing tunnel reads as immutable.
		frm.set_df_property("virtual_machine", "read_only", 1);
		frm.set_df_property("client_public_key", "read_only", 1);
		add_buttons(frm);
	},
});

function add_buttons(frm) {
	const status = frm.doc.status;
	if (status === "Pending") {
		// Saved Frappe-side but not yet on the host. Bring up mints the host key
		// and opens the WireGuard interface scoped to this one VM.
		frappe.atlas.add_primary(frm, "Bring up", () => bring_up(frm));
		frappe.atlas.add_danger(frm, "Revoke", () => confirm_revoke(frm));
	} else if (status === "Active") {
		// Live. The client needs the connection details (Atlas never had the
		// client's private key, so the config carries a placeholder).
		frappe.atlas.add_primary(frm, "Show client config", () => show_client_config(frm));
		// Re-apply re-runs bring-up: re-mints/reuses the host key and re-lays the
		// nft isolation, e.g. after a host reboot left the interface gone.
		frappe.atlas.add_success(frm, "Re-apply", () => bring_up(frm));
		frappe.atlas.add_danger(frm, "Revoke", () => confirm_revoke(frm));
	}
	// Revoked: terminal, no actions — only the intro.
}

function render_intro(frm) {
	frm.set_intro("");
	if (frm.is_new()) {
		frm.set_intro(
			__(
				"On your machine run <code>wg genkey | tee privatekey | wg pubkey > publickey</code>, paste the <b>public</b> key below, pick the VM, and Save. Then Bring up opens the tunnel and Show client config gives you the connection details. Your private key never leaves your machine."
			),
			"blue"
		);
		return;
	}
	const status = frm.doc.status;
	if (status === "Pending") {
		frm.set_intro(
			__("Saved but not yet on the host. Click Bring up to open the tunnel."),
			"orange"
		);
	} else if (status === "Active") {
		frm.set_intro(
			__(
				"Tunnel is live. Click Show client config for the connection details and setup steps."
			),
			"green"
		);
	} else if (status === "Revoked") {
		frm.set_intro(
			__(
				"This tunnel was revoked and torn down on the host. Create a new tunnel to reconnect."
			),
			"red"
		);
	}
}

function bring_up(frm) {
	frm.call("bring_up").then(({ message }) => {
		frappe.atlas.task_started(frm, __("Bring up tunnel"), message);
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
					"This tunnel reaches <b>{0}</b> at <code>{1}</code> and nothing else. Save the block below as <code>/etc/wireguard/atlas.conf</code> and replace <code>&lt;your client private key&gt;</code> with the contents of your <code>privatekey</code> file — Atlas never sees it.",
					[
						frappe.utils.escape_html(frm.doc.virtual_machine),
						frappe.utils.escape_html(cfg.allowed_ips),
					]
				)}</p>`,
			},
			{
				fieldname: "config",
				fieldtype: "Code",
				label: __("atlas.conf"),
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
		title: __("Revoke this tunnel?"),
		body_html: `<p>${__(
			"Tears down the WireGuard interface on the host and frees its slot. The client loses access immediately. This cannot be undone — issue a new tunnel to reconnect."
		)}</p>`,
		match_string: frm.doc.virtual_machine,
		match_label: __("Type the Virtual Machine name to confirm"),
		proceed_label: __("Revoke"),
		proceed() {
			frm.call("revoke").then(({ message }) => {
				frappe.atlas.task_started(frm, __("Revoke tunnel"), message);
			});
		},
	});
}
