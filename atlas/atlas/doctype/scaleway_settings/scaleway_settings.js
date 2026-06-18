// Scaleway Settings — Single. Exposes a Test Connection action under
// Actions ▾ and filters the size/image link queries to Scaleway rows.
// Mirrors DigitalOcean Settings: no auto-painted credential chip — the
// operator verifies via Test Connection, which surfaces a toast.

frappe.ui.form.on("Scaleway Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
		frm.set_query("default_size", () => ({
			filters: { provider_type: "Scaleway", enabled: 1 },
		}));
		frm.set_query("default_image", () => ({
			filters: { provider_type: "Scaleway", enabled: 1 },
		}));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			frappe.show_alert({
				message: __("OK: {0}", [message.account_label || __("Scaleway")]),
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
