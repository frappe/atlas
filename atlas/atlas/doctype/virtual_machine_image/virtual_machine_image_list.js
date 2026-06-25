frappe.listview_settings["Virtual Machine Image"] = {
	add_fields: ["is_active", "title"],

	onload(listview) {
		// Desk equivalent of `atlas.bootstrap.ensure_image`: seed the canonical
		// base-image rows (server + minimal) from pinned constants so an operator
		// never hand-types kernel/rootfs URLs and digests. Idempotent server-side.
		listview.page.add_inner_button(__("Seed default images"), () =>
			seed_default_images(listview)
		);
	},

	get_indicator(doc) {
		if (!doc.is_active) {
			return [__("Inactive"), "grey", "is_active,=,0"];
		}
		return [__("Active"), "green", "is_active,=,1"];
	},

	formatters: {
		image_name(value, _df, doc) {
			if (!doc.title) return value;
			return `${value} · ${doc.title}`;
		},
	},
};

function seed_default_images(listview) {
	frappe
		.call(
			"atlas.atlas.doctype.virtual_machine_image.virtual_machine_image.seed_default_images"
		)
		.then(({ message }) => {
			const created = (message && message.created) || [];
			const skipped = (message && message.skipped) || [];
			const parts = [];
			if (created.length) parts.push(__("Created: {0}", [created.join(", ")]));
			if (skipped.length) parts.push(__("Already present: {0}", [skipped.join(", ")]));
			frappe.show_alert({
				message: parts.join(" · ") || __("Nothing to seed."),
				indicator: created.length ? "green" : "blue",
			});
			listview.refresh();
		});
}
