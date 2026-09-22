const HOST_REGISTRATION_METHOD = "atlas.metal_server.doctype.metal_server.metal_server";
const HOST_INSPECTION_POLL_MILLISECONDS = 2_000;
const HOST_INSPECTION_TIMEOUT_MILLISECONDS = 300_000;
const HOST_REGISTRATION_FIELDS = [
	"public_ipv4_address",
	"private_ipv4_address",
	"storage_pool_device",
	"disk_image_size_gib",
	"provider_server_id",
];

class HostRegistrationDialog {
	constructor() {
		this.dialog = new frappe.ui.Dialog({
			title: __("Add Server"),
			fields: [
				{ fieldname: "guide", fieldtype: "HTML" },
				{
					fieldname: "public_ipv4_address",
					fieldtype: "Data",
					label: __("Public IPv4 Address"),
				},
				{
					fieldname: "private_ipv4_address",
					fieldtype: "Data",
					label: __("Private IPv4 Address"),
				},
				{ fieldname: "result", fieldtype: "HTML" },
				{ fieldname: "disk_warning", fieldtype: "HTML" },
				{
					fieldname: "storage_pool_device",
					fieldtype: "Select",
					label: __("Storage Pool Device"),
					description: __("Atlas destroys all data on this disk."),
					onchange: () => this.render_disk_warning(),
				},
				{
					fieldname: "disk_image_size_gib",
					fieldtype: "Int",
					label: __("Disk Image Size (GiB)"),
				},
				{
					fieldname: "provider_server_id",
					fieldtype: "Data",
					label: __("Provider Server ID"),
					description: __("The ID of this host at your provider. Atlas suggests one."),
				},
			],
		});
		this.show_addresses_step();
		this.dialog.show();
	}

	show_addresses_step() {
		this.set_step(
			["public_ipv4_address", "private_ipv4_address"],
			__("Step 1 of 4: Enter the host addresses"),
			`<ul>
				<li>${__("The host runs Ubuntu or Debian on x86_64 with KVM.")}</li>
				<li>${__("Atlas can connect as root to the public address with its SSH key.")}</li>
				<li>${__("Private address on the provider network with MTU ≥ 1340.")}</li>
				<li>${__("One whole raw disk is empty for the storage pool.")}</li>
			</ul>`
		);
		this.set_actions(__("Inspect"), () => this.inspect());
	}

	inspect() {
		const values = this.dialog.get_values();
		if (!values) return;

		frappe
			.call({
				method: `${HOST_REGISTRATION_METHOD}.inspect_host`,
				args: {
					public_ipv4_address: values.public_ipv4_address,
					private_ipv4_address: values.private_ipv4_address,
				},
				freeze: true,
			})
			.then(({ message }) => {
				this.inspection_id = message;
				this.wait(
					__("Step 2 of 4: Inspecting the host"),
					__("Atlas is reading the host facts...")
				);
			});
	}

	wait(title, message) {
		this.started_at = Date.now();
		this.set_step([], title, "");
		this.set_result(`<p class="text-muted">${message}</p>`);
		this.set_actions(null);
		this.poll();
	}

	poll() {
		frappe
			.call({
				method: `${HOST_REGISTRATION_METHOD}.get_host_inspection`,
				args: { inspection_id: this.inspection_id },
			})
			.then(({ message: state }) => {
				if (state.status !== "Inspecting") {
					this.show_inspection_result(state);
				} else if (Date.now() - this.started_at > HOST_INSPECTION_TIMEOUT_MILLISECONDS) {
					this.show_failure(__("The inspection did not finish. Read the worker log."));
				} else {
					setTimeout(() => this.poll(), HOST_INSPECTION_POLL_MILLISECONDS);
				}
			});
	}

	show_inspection_result(state) {
		if (state.status === "Failed") {
			this.show_failure(state.error);
			return;
		}

		this.state = state;
		if (state.failures.length) {
			this.set_step([], __("Step 2 of 4: The host is not ready"), "");
			this.set_result(this.render_failures() + this.render_facts());
			this.set_actions(__("Back"), () => this.show_addresses_step());
			return;
		}
		this.show_disk_step();
	}

	show_disk_step() {
		const free_devices = this.free_devices;
		const title = __("Step 3 of 4: Choose the storage disk");
		if (free_devices.length) {
			const selected = this.dialog.get_value("storage_pool_device");
			this.set_step(["storage_pool_device"], title, "");
			this.set_result(this.render_disks());
			this.dialog.set_df_property("storage_pool_device", "options", free_devices);
			this.dialog.set_value(
				"storage_pool_device",
				free_devices.includes(selected) ? selected : this.default_device
			);
			this.render_disk_warning();
			this.set_actions(
				__("Next"),
				() => this.dialog.get_values() && this.show_review_step(),
				__("Back"),
				() => this.show_addresses_step()
			);
			return;
		}

		if (this.state.can_create_disk_image) {
			this.set_step(["disk_image_size_gib"], title, "");
			this.set_result(this.render_disks());
			this.dialog.set_df_property(
				"disk_image_size_gib",
				"description",
				__("Atlas creates {0} with this size. {1} GiB is free.", [
					frappe.utils.escape_html(this.state.disk_image_path),
					this.state.report.image_available_gib,
				])
			);
			this.dialog.set_value("disk_image_size_gib", this.state.disk_image_default_size_gib);
			this.dialog.fields_dict.disk_warning.$wrapper.html(this.disk_image_warning);
			this.set_actions(
				__("Create Disk Image"),
				() => this.create_disk_image(),
				__("Back"),
				() => this.show_addresses_step()
			);
			return;
		}

		this.set_step([], title, "");
		this.set_result(
			`<div class="alert alert-danger">${__(
				"The host has no free raw disk and no free space for a disk image."
			)}</div>${this.render_disks()}`
		);
		this.set_actions(__("Back"), () => this.show_addresses_step());
	}

	create_disk_image() {
		const values = this.dialog.get_values();
		if (!values) return;

		frappe
			.call({
				method: `${HOST_REGISTRATION_METHOD}.create_host_disk_image`,
				args: { inspection_id: this.inspection_id, size_gib: values.disk_image_size_gib },
				freeze: true,
			})
			.then(() =>
				this.wait(
					__("Step 3 of 4: Creating the disk image"),
					__("Atlas is creating the disk image and will inspect the host again...")
				)
			);
	}

	show_review_step() {
		const storage_pool_device = this.dialog.get_value("storage_pool_device");
		this.set_step(["provider_server_id"], __("Step 4 of 4: Review the host"), "");
		this.set_result(
			`<div class="alert alert-danger">${__(
				"Atlas destroys all data on {0} when it creates the storage pool.",
				[frappe.utils.escape_html(storage_pool_device).bold()]
			)}</div>${this.render_facts(storage_pool_device)}`
		);
		if (!this.dialog.get_value("provider_server_id")) {
			this.dialog.set_value("provider_server_id", this.suggested_provider_server_id);
		}
		this.set_actions(
			__("Create Metal Server"),
			() => this.register(storage_pool_device),
			__("Back"),
			() => this.show_disk_step()
		);
	}

	show_failure(message) {
		this.set_step([], __("Inspection failed"), "");
		this.set_result(`<pre class="text-danger">${frappe.utils.escape_html(message)}</pre>`);
		this.set_actions(__("Back"), () => this.show_addresses_step());
	}

	register(storage_pool_device) {
		frappe
			.call({
				method: `${HOST_REGISTRATION_METHOD}.register_host`,
				args: {
					inspection_id: this.inspection_id,
					storage_pool_device,
					provider_server_id: this.dialog.get_value("provider_server_id"),
				},
				freeze: true,
				freeze_message: __("Creating Metal Server..."),
			})
			.then(({ message }) => {
				this.dialog.hide();
				frappe.set_route("Form", "Metal Server", message);
			});
	}

	get free_devices() {
		return this.state.report.disks
			.filter((disk) => !disk.busy_reason)
			.map((disk) => disk.device);
	}

	get suggested_provider_server_id() {
		const date = moment.utc().format("DD-MM-YYYY");
		return `generic-${date}-${frappe.utils.get_random(6).toLowerCase()}`;
	}

	get default_device() {
		const raw_disks = this.free_devices.filter(
			(device) => device !== this.state.disk_image_path
		);
		return raw_disks[0] || this.free_devices[0];
	}

	get disk_image_warning() {
		return `<div class="alert alert-warning">${__(
			"Prefer a raw disk for better VM performance. Use a disk image only when a dedicated raw disk is not available."
		)}</div>`;
	}

	render_disk_warning() {
		const is_disk_image =
			this.dialog.get_value("storage_pool_device") === this.state?.disk_image_path;
		this.dialog.fields_dict.disk_warning.$wrapper.html(
			is_disk_image ? this.disk_image_warning : ""
		);
	}

	render_failures() {
		const failures = this.state.failures.map(
			(failure) => `<li>${frappe.utils.escape_html(failure)}</li>`
		);
		return `<div class="alert alert-danger"><ul>${failures.join("")}</ul></div>`;
	}

	render_facts(storage_pool_device) {
		const report = this.state.report;
		const rows = [
			[__("Host Name"), report.hostname],
			[__("Machine"), report.machine],
			[__("Operating System"), `${report.os} ${report.os_version}`],
			[__("CPU and Memory"), `${report.cpu_count} CPU, ${report.memory_mib} MiB`],
			[__("KVM"), report.has_kvm ? __("Yes") : __("No")],
			[__("Public Interface"), report.public_network_interface || __("Not found")],
			[
				__("Private Interface"),
				report.private_network_interface
					? `${report.private_network_interface} (MTU ${report.private_network_mtu})`
					: __("Not found"),
			],
			[__("Server Size"), this.state.server_size_name],
			[__("Server Image"), this.state.server_image_name],
		];
		if (storage_pool_device) {
			rows.push([__("Storage Pool Device"), storage_pool_device]);
		}
		return `<table class="table table-bordered">
			${rows
				.map(
					([label, value]) =>
						`<tr><th>${label}</th><td>${frappe.utils.escape_html(
							String(value)
						)}</td></tr>`
				)
				.join("")}
		</table>`;
	}

	render_disks() {
		const escape = frappe.utils.escape_html;
		const disks = this.state.report.disks.map(
			(disk) =>
				`<tr><td>${escape(disk.device)}</td><td>${disk.size_gib} GiB</td><td>${escape(
					disk.busy_reason || __("Free")
				)}</td></tr>`
		);
		if (!disks.length) {
			return `<p class="text-muted">${__("The host has no whole disk.")}</p>`;
		}
		return `<table class="table table-bordered">
			<tr><th>${__("Disk")}</th><th>${__("Size")}</th><th>${__("State")}</th></tr>
			${disks.join("")}
		</table>`;
	}

	set_step(visible_fields, title, guide) {
		HOST_REGISTRATION_FIELDS.forEach((field) => {
			const is_visible = visible_fields.includes(field);
			this.dialog.set_df_property(field, "hidden", !is_visible);
			this.dialog.set_df_property(
				field,
				"reqd",
				is_visible && field !== "provider_server_id"
			);
		});
		this.dialog.fields_dict.guide.$wrapper.html(`<h5>${title}</h5>${guide}`);
		this.dialog.fields_dict.disk_warning.$wrapper.html("");
		this.set_result("");
	}

	set_result(html) {
		this.dialog.fields_dict.result.$wrapper.html(html);
	}

	set_actions(primary_label, primary_action, secondary_label, secondary_action) {
		const primary_button = this.dialog.get_primary_btn();
		if (primary_label) {
			this.dialog.set_primary_action(primary_label, primary_action);
		}
		primary_button.prop("disabled", !primary_label);

		const secondary_button = this.dialog.get_secondary_btn();
		if (secondary_label) {
			this.dialog.set_secondary_action_label(secondary_label);
			this.dialog.set_secondary_action(secondary_action);
		}
		secondary_button.toggleClass("hide", !secondary_label);
	}
}

frappe.listview_settings["Metal Server"] = {
	primary_action() {
		frappe.db.get_single_value("Atlas Settings", "server_provider").then((provider) => {
			if (provider === "Generic") {
				new HostRegistrationDialog();
				return;
			}
			frappe.new_doc("Metal Server");
		});
	},
};
