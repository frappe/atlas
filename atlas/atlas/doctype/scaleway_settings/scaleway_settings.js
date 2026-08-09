// Scaleway Settings — Single. Exposes a Test Connection action under
// Actions ▾. Mirrors DigitalOcean Settings: no auto-painted credential chip —
// the operator verifies via Test Connection, which surfaces a toast. The
// default size/image are no longer fields here — they live as `is_default`
// on Provider Size / Provider Image.

frappe.ui.form.on("Scaleway Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
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
