const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function form({ isNew = false, status = "Pending", powerState = "On", confirm = true } = {}) {
	let handlers;
	const buttons = new Map();
	const calls = [];
	const alerts = [];
	const frappe = {
		ui: { form: { on: (_name, value) => (handlers = value) } },
		db: { get_single_value: async () => "Redfish" },
		confirm: (_message, action) => confirm && action(),
		show_alert: (message) => alerts.push(message),
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
		add_custom_button: (label, action, group) => {
			assert.equal(group, "Actions");
			buttons.set(label, action);
		},
		call: async (request) => calls.push(request),
		reload_doc: () => calls.push("reload"),
	};
	handlers.refresh(frm);
	return { buttons, calls, alerts };
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

test("Power On is available for an Off BMC and uses the saved-document method", async () => {
	const { buttons, calls } = form({ powerState: "Off", status: "Stopped" });
	assert.ok(buttons.has("Power On"));
	buttons.get("Power On")();
	await Promise.resolve();
	assert.equal(calls[0].method, "poweron_server");
	assert.equal(calls[1], "reload");
	for (const powerState of ["On", "PoweringOn", "PoweringOff", ""]) {
		assert.ok(!form({ powerState }).buttons.has("Power On"));
	}
});

test("Power Off confirms graceful shutdown and is offered only for On", async () => {
	const { buttons, calls } = form();
	buttons.get("Power Off")();
	await Promise.resolve();
	assert.equal(calls[0].method, "poweroff_server");
	const cancelled = form({ confirm: false });
	cancelled.buttons.get("Power Off")();
	assert.deepEqual(cancelled.calls, []);
	for (const powerState of ["Off", "PoweringOn", "PoweringOff", ""]) {
		assert.ok(!form({ powerState }).buttons.has("Power Off"));
	}
});

test("Reboot confirms a graceful restart and is offered only for On", async () => {
	const { buttons, calls } = form();
	buttons.get("Reboot")();
	await Promise.resolve();
	assert.equal(calls[0].method, "reboot_server");
	assert.equal(calls[1], "reload");
	const cancelled = form({ confirm: false });
	cancelled.buttons.get("Reboot")();
	assert.deepEqual(cancelled.calls, []);
	for (const powerState of ["Off", "PoweringOn", "PoweringOff", ""]) {
		assert.ok(!form({ powerState }).buttons.has("Reboot"));
	}
});
