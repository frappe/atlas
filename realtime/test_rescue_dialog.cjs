const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const text = fs.readFileSync(path.join(__dirname, "../atlas/vm/doctype/virtual_machine/virtual_machine.js"), "utf8");
const source = text.slice(text.indexOf("function rescueRequest("));
const settle = () => new Promise(resolve => setImmediate(resolve));
const normal = {
	desired: { rescue: { enabled: false }, rescue_generation: 0 },
	observed: { state: "running", rescue_generation: 0 },
};

async function open(state = normal, offline = false) {
	const view = { state: structuredClone(state), offline, timers: new Set(), requests: [], confirmations: [] };
	const context = {
		__: (message, args = []) => message.replace(/\{(\d+)\}/g, (_, index) => args[index]),
		openConsole() {},
		setInterval(callback) { view.poll = callback; view.timers.add(callback); return callback; },
		clearInterval(timer) { view.timers.delete(timer); },
		frappe: {
			ui: { Dialog: class {
				constructor(config) {
					view.dialog = this;
					this.config = config;
					this.fields_dict = { status: { $wrapper: { html(value) { view.html = value; } } } };
				}
				show() {}
				get_value() { return view.selection || ""; }
				set_value(name, value) { view.selection = value; }
				set_df_property(name, property, value) {
					if (name === "operation" && property === "options") view.options = value;
					if (name === "retry" && property === "hidden") view.retryHidden = value;
				}
				get_primary_btn() { return { prop: (_, value) => { view.disabled = value; } }; }
			} },
			utils: { escape_html: value => value.replaceAll("<", "&lt;").replaceAll(">", "&gt;") },
			confirm(warning, accept, cancel) { view.confirmations.push({ warning, accept, cancel }); },
			show_alert() {},
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	context.showRescueDialog({
		doc: {}, reload_doc() {},
		call(request) {
			if (request.method === "set_rescue") {
				view.requests.push(JSON.parse(JSON.stringify(request.args)));
				return view.mutation || Promise.resolve({});
			}
			// Frappe's thenable does not supply Promise.finally.
			return { then(resolve, reject) {
				return (view.offline ? Promise.reject(new Error("offline")) : Promise.resolve({ message: view.state })).then(resolve, reject);
			} };
		},
	});
	await settle();
	view.retry = () => view.dialog.config.fields.find(field => field.fieldname === "retry").click();
	view.submit = operation => view.dialog.config.primary_action({ operation });
	return view;
}

async function check(state, expected, excluded, failFirst = false) {
	const view = await open(state, failFirst);
	if (failFirst) {
		assert(view.disabled);
		assert(view.html.includes("state is unavailable"));
		assert.equal(view.timers.size, 0);
		view.offline = false;
		await view.retry();
	}
	const labels = view.options.map(option => option.label);
	for (const label of expected) assert(labels.includes(label), label);
	for (const label of excluded) assert(!labels.includes(label), label);
	assert(!view.html.includes("<script>"));
	assert.equal(view.timers.size, 1);
	assert.equal(view.retryHidden, 1);
	view.offline = true;
	await view.poll();
	assert(view.disabled);
	assert.equal(view.timers.size, 0);
	assert.equal(view.retryHidden, 0);
	view.offline = false;
	await view.retry();
	assert.equal(view.timers.size, 1);
	view.dialog.onhide();
	await view.retry();
	assert.equal(view.timers.size, 0);
}

(async () => {
	await check(normal, ["Enter rescue"], ["Exit rescue"]);
	await check(normal, ["Enter rescue"], [], true);
	const active = structuredClone(normal);
	active.desired.rescue = { enabled: true };
	await check(active, ["Exit rescue"], ["Enter rescue"]);
	const failed = structuredClone(active);
	failed.observed.error = { message: "<script>bad</script>" };
	await check(failed, ["Exit rescue"], ["Enter rescue"]);

	for (const [state, operation, request] of [
		[normal, "enter", { enabled: true }],
		[active, "exit", { enabled: false }],
	]) {
		const view = await open(state);
		view.submit(operation);
		view.submit(operation);
		assert.equal(view.confirmations.length, 1, "Repeated clicks must not open competing confirmations");
		assert(view.disabled);
		view.confirmations[0].cancel();
		assert.equal(view.requests.length, 0);
		assert(!view.disabled);
		view.submit(operation);
		await view.poll();
		let release;
		view.mutation = new Promise(resolve => { release = resolve; });
		const accepted = view.confirmations[1].accept();
		view.submit(operation);
		assert.equal(view.confirmations.length, 2);
		assert.deepEqual(view.requests, [request], "Confirmation must retain the selected action");
		release({});
		await accepted;
		view.dialog.onhide();
	}
	const pending = structuredClone(active);
	pending.desired.rescue_generation++;
	const view = await open(pending);
	view.submit("exit");
	assert.equal(view.confirmations.length, 0);
	view.state.observed.error = { message: "retry" };
	await view.poll();
	view.submit("exit");
	view.mutation = Promise.reject(new Error("request failed"));
	await assert.rejects(view.confirmations[0].accept(), /request failed/);
	assert(!view.disabled, "A failed request must allow retry");
	view.dialog.onhide();
	console.log("PASS rescue states, escaping, polling, entry/exit, cancellation, duplicate suppression and failed-request retry");
})().catch(error => { console.error(error); process.exitCode = 1; });
