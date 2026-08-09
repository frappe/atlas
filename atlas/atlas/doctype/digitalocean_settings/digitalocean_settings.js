// DigitalOcean Settings — Single. Exposes a Test Connection action under
// Actions ▾. Per spec (10-desk-ui.md § DigitalOcean Settings) the form carries
// no auto-painted credential chip — the operator verifies via Test Connection,
// which surfaces its result as a toast. The default size/image are no longer
// fields here — they live as `is_default` on Provider Size / Provider Image.

frappe.ui.form.on("DigitalOcean Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			frappe.show_alert({
				message: __("OK: {0}", [message.account_label || __("DigitalOcean")]),
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
