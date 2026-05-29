frappe.listview_settings["Virtual Machine"] = {
	add_fields: ["status", "ipv6_address", "name", "title"],

	formatters: {
		// Frappe renders the title (the list "subject" column) inside an
		// anchor where the value is plain-text escaped — HTML in the formatter
		// would show as literal angle brackets. We append the short ID with a
		// `·` separator instead and let the muted styling come from the IPv6
		// + ID columns next to it.
		title(value, _df, doc) {
			const short = (doc.name || "").slice(0, 8);
			const label = value || "(no title)";
			return `${label} · ${short}`;
		},

		ipv6_address(value) {
			if (!value) return "";
			const safe = frappe.utils.escape_html(value);
			return `<span class="atlas-ipv6-chip" data-ipv6="${safe}" title="${__("Copy ssh root@…")}">
				<code class="text-muted small">[${safe}]</code>
				<span class="atlas-copy-icon">📋</span>
			</span>`;
		},
	},

	get_indicator(doc) {
		const config = {
			Pending: ["Pending", "orange", "status,=,Pending"],
			Running: ["Running", "green", "status,=,Running"],
			Paused: ["Paused", "yellow", "status,=,Paused"],
			Stopped: ["Stopped", "grey", "status,=,Stopped"],
			Failed: ["Failed", "red", "status,=,Failed"],
			Terminated: ["Terminated", "darkgrey", "status,=,Terminated"],
		}[doc.status];
		return config ? [__(config[0]), config[1], config[2]] : null;
	},

	onload(listview) {
		// Click handler for IPv6 copy chip. Bound on the list wrapper so it
		// survives rerenders.
		listview.$result.on("click", ".atlas-ipv6-chip", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const ipv6 = event.currentTarget.dataset.ipv6;
			if (!ipv6) return;
			const command = `ssh root@${ipv6}`;
			navigator.clipboard.writeText(command).then(() => {
				frappe.show_alert({
					message: __("SSH command copied: {0}", [`<code>${frappe.utils.escape_html(command)}</code>`]),
					indicator: "green",
				}, 4);
			}).catch(() => {
				frappe.show_alert({
					message: __("Could not copy to clipboard."),
					indicator: "orange",
				});
			});
		});
	},
};
