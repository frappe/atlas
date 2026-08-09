// Central Settings — Single. Connects this Atlas to the global Central control
// plane (spec/16-central.md). Actions ▾ carries Test Connection. Results surface
// as toasts, matching the other Atlas Settings singles (no auto-painted
// credential chip). Registration is Central-initiated now (spec/21-tunnel.md) —
// there is no Register button here.

frappe.ui.form.on("Central Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () =>
			run(frm, "test_connection", (m) =>
				m.ok ? __("OK: {0}", [m.label || "Central"]) : null
			)
		);
	},
});

// Call a whitelisted method, render the result as a toast. `ok_message`
// returns the green-toast text, or null when the message carries an error.
function run(frm, method, ok_message) {
	frappe.show_alert({ message: __("Working…"), indicator: "blue" });
	frm.call(method).then(({ message }) => {
		const error = message && message.error;
		const text = error ? null : ok_message(message);
		frappe.show_alert({
			message: error ? __("Failed: {0}", [error]) : text,
			indicator: error ? "red" : "green",
		});
		frm.reload_doc(); // pick up status written server-side
	});
}
