// Lets Encrypt Settings — Single. ACME account config + the Test Connection
// button the deleted TLS Provider form used to own.

frappe.ui.form.on("Lets Encrypt Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			const label = message.account_label || __("Let's Encrypt");
			frappe.show_alert({ message: __("OK: {0}", [label]), indicator: "green" });
		} else {
			frappe.show_alert({
				message: __("Failed: {0}", [message.error || __("unknown error")]),
				indicator: "red",
			});
		}
	});
}
