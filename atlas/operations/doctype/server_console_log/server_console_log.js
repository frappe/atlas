// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

/* global Terminal, FitAddon */

const TERMINAL_ASSETS = [
	"/assets/atlas/vendor/console/xterm.min.css",
	"/assets/atlas/vendor/console/xterm.min.js",
	"/assets/atlas/vendor/console/addon-fit.min.js",
];
const ANSI_ESCAPE = /\x1b\[[0-9;?]*[A-Za-z]/g;

// Updates values in place. A form reload while a row is open breaks the open row form.
frappe.ui.form.on("Server Console Log", {
	refresh(frm) {
		const grid = frm.fields_dict.results.grid;
		grid.update_docfield_property("output", "formatter", (value) =>
			frappe.form.formatters.Code((value || "").replace(ANSI_ESCAPE, ""))
		);
		frm.refresh_field("results");

		frappe.realtime.off("server_console_log_update");
		frappe.realtime.on("server_console_log_update", ({ name, log, results }) => {
			if (name !== frm.doc.name) return;

			Object.assign(frm.doc, log);
			Object.keys(log).forEach((field) => frm.refresh_field(field));
			frm.refresh_header();

			results.forEach((values) => {
				Object.assign(locals["Server Console Log Server"][values.name], values);
				const row = grid.grid_rows_by_docname[values.name];
				["status", "exit_code", "output"].forEach((field) => row.refresh_field(field));
				row.grid_form?.terminal?.write_output(values.output);
			});
		});
	},
});

frappe.ui.form.on("Server Console Log Server", {
	async form_render(frm, doctype, name) {
		const grid_form = frm.fields_dict.results.grid.grid_rows_by_docname[name].grid_form;
		grid_form.fields_dict.output.$wrapper.hide();
		if (grid_form.terminal) return;

		await frappe.require(TERMINAL_ASSETS);
		grid_form.terminal = new OutputTerminal(grid_form.fields_dict.output_terminal.$wrapper);
		grid_form.terminal.write_output(locals[doctype][name].output);
	},
});

// Shows the raw output with its colors. The output only grows, so each update writes the new part.
class OutputTerminal {
	constructor($wrapper) {
		const frame = $(
			'<div style="position: relative; background: #000; padding: var(--padding-md); border-radius: 8px">'
		).appendTo($wrapper.empty());
		$(`<button class="btn btn-default icon-btn" title="${__("Copy to Clipboard")}"
			style="position: absolute; top: 8px; right: 8px; z-index: 11">
			${frappe.utils.icon("copy", "sm")}
		</button>`)
			.on("click", () =>
				frappe.utils.copy_to_clipboard(this.output.replace(ANSI_ESCAPE, ""))
			)
			.appendTo(frame);
		const container = $('<div style="height: 400px">').appendTo(frame).get(0);

		this.terminal = new Terminal({
			convertEol: true,
			disableStdin: true,
			cursorInactiveStyle: "none",
			fontSize: 15,
			lineHeight: 1.2,
		});
		const fit = new FitAddon.FitAddon();
		this.terminal.loadAddon(fit);
		this.terminal.open(container);
		fit.fit();
		// Copies a selection when the mouse is released, like ttyd.
		container.addEventListener("mouseup", () => {
			if (this.terminal.hasSelection()) {
				frappe.utils.copy_to_clipboard(this.terminal.getSelection());
			}
		});
		this.output = "";
	}

	write_output(output) {
		this.terminal.write((output || "").slice(this.output.length));
		this.output = output || "";
	}
}
