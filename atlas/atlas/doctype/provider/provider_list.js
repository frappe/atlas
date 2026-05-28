frappe.listview_settings["Provider"] = {
	add_fields: ["is_active", "provider_type"],

	get_indicator(doc) {
		if (!doc.is_active) {
			return [__("Inactive"), "grey", "is_active,=,0"];
		}
		return [__("Active"), "green", "is_active,=,1"];
	},

	formatters: {
		provider_name(value, _df, doc) {
			if (!doc.provider_type) return value;
			return `${value} · ${doc.provider_type}`;
		},
	},
};
