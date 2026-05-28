// DigitalOcean Settings — Single. Paints an auto-refreshing credential
// indicator and exposes a Test Connection action under Actions ▾.

frappe.ui.form.on("DigitalOcean Settings", {
	refresh(frm) {
		paint_credential_indicator(frm);
		frappe.atlas.add_action(frm, "Test Connection", () => run_test_connection(frm));
		// Filter size/image link queries to DigitalOcean rows only.
		frm.set_query("default_size", () => ({
			filters: {provider_type: "DigitalOcean", enabled: 1},
		}));
		frm.set_query("default_image", () => ({
			filters: {provider_type: "DigitalOcean", enabled: 1},
		}));
	},
});


function paint_credential_indicator(frm) {
	frm.dashboard.clear_headline();
	frm.call("credential_check").then(({message}) => {
		if (message && message.ok) {
			const label = message.account_label || __("DigitalOcean");
			const detail = message.rate_remaining != null
				? __("{0} · {1}/{2} requests remaining", [label, message.rate_remaining, message.rate_limit])
				: label;
			frm.dashboard.set_headline_alert(
				`<div class="indicator green">${frappe.utils.escape_html(detail)}</div>`,
			);
		} else {
			const error = message ? message.error || __("Authentication failed") : __("Authentication failed");
			frm.dashboard.set_headline_alert(
				`<div class="indicator red">${frappe.utils.escape_html(error)}</div>`,
			);
		}
	});
}


function run_test_connection(frm) {
	frappe.show_alert({message: __("Testing connection…"), indicator: "blue"});
	frm.call("test_connection").then(({message}) => {
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
