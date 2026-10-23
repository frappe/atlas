import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

test("VM selection is conditional and follows the source host", () => {
	const metadata = JSON.parse(
		readFileSync(new URL("./metal_server_drain.json", import.meta.url), "utf8")
	);
	const table = metadata.fields.find((field) => field.fieldname === "virtual_machines");
	for (const field of ["depends_on", "mandatory_depends_on"]) {
		for (const checked of [0, 1]) {
			assert.equal(
				Boolean(
					runInNewContext(table[field].slice(5), {
						doc: { move_only_selected_vms: checked },
					})
				),
				Boolean(checked)
			);
		}
	}

	let handlers;
	let query;
	runInNewContext(readFileSync(new URL("./metal_server_drain.js", import.meta.url), "utf8"), {
		frappe: { ui: { form: { on: (_doctype, events) => (handlers = events) } } },
	});
	const form = {
		doc: { source_metal_server: "host-1" },
		set_query(fieldname, parentfield, callback) {
			assert.equal(fieldname, "virtual_machine");
			assert.equal(parentfield, "virtual_machines");
			query = callback;
		},
	};
	handlers.setup(form);
	for (const source of ["host-1", "host-2"]) {
		form.doc.source_metal_server = source;
		assert.deepEqual(JSON.parse(JSON.stringify(query())), {
			filters: { server: source, is_draft: 0, is_terminating: 0 },
		});
	}
});
