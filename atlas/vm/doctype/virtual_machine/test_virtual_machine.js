import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

test("Snapshot Upload renders zero and partial progress without polling", () => {
	const source = readFileSync(new URL("./virtual_machine.js", import.meta.url), "utf8");
	const metadata = JSON.parse(
		readFileSync(new URL("./virtual_machine.json", import.meta.url), "utf8")
	);
	let handlers;
	let formatter;
	runInNewContext(source, {
		frappe: { ui: { form: { on: (_doctype, events) => (handlers = events) } } },
		__: (message) => message,
	});
	handlers.refresh({
		doc: { name: "vm-1", current_state: "stopped" },
		is_new: () => false,
		disable_save() {},
		add_custom_button() {},
		set_df_property: (fieldname, property, value) => {
			assert.equal(fieldname, "snapshot_upload_percent");
			assert.equal(property, "formatter");
			formatter = value;
		},
	});
	const field = metadata.fields.find((field) => field.fieldname === "snapshot_upload_percent");
	const visible = (value) =>
		runInNewContext(field.depends_on.slice(5), { doc: { snapshot_upload_percent: value } });
	assert.equal(visible(null), false);
	for (const percent of ["0", "42", "99"]) {
		assert.equal(visible(percent), true);
		assert.match(formatter(percent), new RegExp(`<progress value="${percent}" max="100"`));
		assert.match(formatter(percent), new RegExp(`${percent}%$`));
	}
});
