frappe.listview_settings["Task"] = {
	add_fields: ["status", "script", "duration_milliseconds"],

	// `get_indicator` no longer needed — DocType `states` array paints the
	// Status column's pill (Pending/Running/Success/Failure) automatically.

	formatters: {
		subject(value, _df, doc) {
			const ms = doc.duration_milliseconds;
			const duration = ms ? `${Math.round(ms / 1000)}s` : "—";
			const label = value || doc.script || doc.name;
			return `${label} · ${duration}`;
		},
	},
};
