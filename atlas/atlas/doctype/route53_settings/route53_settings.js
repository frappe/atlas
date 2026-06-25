// Route53 Settings — Single. Holds the Route 53 credentials, and the Test
// Connection button the deleted Domain Provider form used to own. The active DNS
// vendor type lives on Atlas Settings (dns_provider_type).

frappe.ui.form.on("Route53 Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			const label = message.account_label || "Route 53";
			frappe.show_alert({ message: __("OK: {0}", [label]), indicator: "green" });
		} else {
			frappe.show_alert({
				message: __("Failed: {0}", [message.error || __("unknown error")]),
				indicator: "red",
			});
		}
	});
}
