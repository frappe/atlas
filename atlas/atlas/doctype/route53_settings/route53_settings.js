// Route53 Settings — Single. Holds the active DNS vendor type + credentials,
// and the Test Connection button the deleted Domain Provider form used to own.

frappe.ui.form.on("Route53 Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			const label = message.account_label || frm.doc.domain_provider_type;
			frappe.show_alert({ message: __("OK: {0}", [label]), indicator: "green" });
		} else {
			frappe.show_alert({
				message: __("Failed: {0}", [message.error || __("unknown error")]),
				indicator: "red",
			});
		}
	});
}
