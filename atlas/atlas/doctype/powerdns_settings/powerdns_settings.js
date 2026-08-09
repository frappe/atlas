// PowerDNS Settings — Single. Holds the Authoritative HTTP API credentials.

frappe.ui.form.on("PowerDNS Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection..."), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			const label = message.account_label || "PowerDNS";
			frappe.show_alert({ message: __("OK: {0}", [label]), indicator: "green" });
		} else {
			frappe.show_alert({
				message: __("Failed: {0}", [message.error || __("unknown error")]),
				indicator: "red",
			});
		}
	});
}
