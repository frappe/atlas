// Central Settings — Single. Connects this Atlas to the global Central control
// plane (spec/16-central.md). Actions ▾ carries Test Connection / Fetch Sizes /
// Fetch Images. Results surface as toasts, matching the other Atlas Settings
// singles (no auto-painted credential chip). Registration is Central-initiated
// now (spec/21-tunnel.md) — there is no Register button here.

frappe.ui.form.on("Central Settings", {
	refresh(frm) {
		frappe.atlas.add_action(frm, "Test Connection", () =>
			run(frm, "test_connection", (m) =>
				m.ok ? __("OK: {0}", [m.label || "Central"]) : null
			)
		);
		frappe.atlas.add_action(frm, "Fetch Sizes", () =>
			run(frm, "fetch_sizes", (m) =>
				__("Sizes: {0} new, {1} updated, {2} disabled", [
					m.inserted,
					m.updated,
					m.disabled,
				])
			)
		);
		frappe.atlas.add_action(frm, "Fetch Images", () =>
			run(frm, "fetch_images", (m) =>
				__("Images: {0} new, {1} updated, {2} disabled", [
					m.inserted,
					m.updated,
					m.disabled,
				])
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
		frm.reload_doc(); // pick up atlas_id / status written server-side
	});
}
