frappe.ui.form.on("Firewall", {
	refresh(frm) {
		render_intro(frm);
		if (frm.is_new()) {
			return;
		}
		// Identity is frozen after insert (validate() enforces it).
		frm.set_df_property("virtual_machine", "read_only", 1);
		// Apply is the explicit verb — a plain Save never touches the host, so the
		// host only changes when the operator/owner asks for it (the bring_up model).
		frappe.atlas.add_primary(frm, "Apply to host", () => apply_to_host(frm));
	},
});

function render_intro(frm) {
	frm.set_intro("");
	if (frm.is_new()) {
		frm.set_intro(
			__(
				"A firewall restricts this VM's <b>public</b> inbound to the ports you list — everything else from the internet is dropped. The VPN tunnel always bypasses it (full access). Pick the VM, add the allowed ports, Save, then Apply to host. An empty list with Enabled on means <b>deny all public</b> (VPN-only). Deleting the firewall reopens the VM to the public internet."
			),
			"blue"
		);
		return;
	}
	if (!frm.doc.enabled) {
		frm.set_intro(
			__(
				"Disabled — the VM is fully public. Tick Enabled and Apply to host to restrict it."
			),
			"orange"
		);
		return;
	}
	if (!(frm.doc.rules || []).length) {
		frm.set_intro(
			__(
				"Deny all public — no public port is open, so only the VPN tunnel reaches this VM. Add ports and Apply to host to open them."
			),
			frm.doc.status === "Active" ? "green" : "orange"
		);
		return;
	}
	if (frm.doc.status === "Active") {
		frm.set_intro(
			__(
				"Enforced on {0}. Public traffic reaches only the listed ports; the VPN tunnel bypasses this. Edit and Apply to host to change.",
				[frm.doc.server]
			),
			"green"
		);
	} else {
		frm.set_intro(
			__("Edited but not yet pushed. Click Apply to host to enforce these rules."),
			"orange"
		);
	}
}

function apply_to_host(frm) {
	const run_sync = () =>
		frm.call("sync").then(({ message }) => {
			if (!message) {
				// sync returns "" when the VM is Terminated (host state already gone).
				frappe.show_alert({
					message: __("Nothing to apply — the VM is terminated."),
					indicator: "orange",
				});
				return;
			}
			frappe.atlas.task_started(frm, __("Apply firewall"), message);
		});
	// Apply what's on screen: persist any pending edits first, then push.
	if (frm.is_dirty()) {
		frm.save().then(run_sync);
	} else {
		run_sync();
	}
}
