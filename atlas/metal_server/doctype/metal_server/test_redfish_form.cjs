const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function form({ isNew = false, status = "Pending", powerState = "On" } = {}) {
	let handlers;
	const buttons = new Map();
	const calls = [];
	const frappe = {
		ui: { form: { on: (_name, value) => (handlers = value) } },
		db: { get_single_value: async () => "Redfish" },
		confirm: (_message, action) => action(),
	};
	vm.runInNewContext(fs.readFileSync(path.join(__dirname, "metal_server.js"), "utf8"), {
		frappe,
		__: (value) => value,
	});
	const frm = {
		doc: { name: "record", status, redfish_power_state: powerState, __onload: { server_provider: "Redfish" } },
		is_new: () => isNew,
		toggle_display: () => {},
		toggle_reqd: () => {},
		disable_save: () => {},
		page: { set_primary_action: (label, action) => buttons.set(label, action) },
		add_custom_button: (label, action) => buttons.set(label, action),
		call: async (request) => calls.push(request),
		reload_doc: () => calls.push("reload"),
	};
	handlers.refresh(frm);
	return { buttons, calls };
}

test("saved Redfish forms refresh BMC state without offering host preparation", async () => {
	const { buttons, calls } = form();
	assert.ok(buttons.has("Refresh Power State"));
	assert.ok(!buttons.has("Retry Provisioning"));
	assert.ok(!buttons.has("Ping Server"));
	buttons.get("Refresh Power State")();
	await Promise.resolve();
	assert.equal(calls[0].method, "refresh_redfish_power_state");
	assert.equal(calls[1], "reload");
});

test("deleted records have no BMC actions", () => {
	assert.equal(form({ status: "Deleted" }).buttons.size, 0);
});

test("new forms keep the single Register action", async () => {
	const { buttons } = form({ isNew: true });
	await Promise.resolve();
	assert.ok(buttons.has("Register"));
	assert.ok(!buttons.has("Refresh Power State"));
});
