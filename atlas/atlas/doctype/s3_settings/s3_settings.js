// S3 Settings — Single. Holds the S3 bucket + credentials for snapshot backups,
// plus the Test Connection button (head_bucket). Credentials stay on the
// controller; hosts only ever receive short-lived presigned URLs.
// See spec/29-snapshot-backup.md.

frappe.ui.form.on("S3 Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
	},
});

function run_test_connection(frm) {
	frappe.show_alert({ message: __("Testing connection…"), indicator: "blue" });
	frm.call("test_connection").then(({ message }) => {
		if (message.ok) {
			frappe.show_alert({ message: __("OK: {0}", [message.detail]), indicator: "green" });
		} else {
			frappe.show_alert({
				message: __("Failed: {0}", [message.detail || __("unknown error")]),
				indicator: "red",
			});
		}
	});
}
