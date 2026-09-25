// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

function storageClusterConfig(values) {
	return {
		storage_node_count: values.storage_node_count,
		replication_factor: values.replication_factor,
		gateway: {
			cpu_millicores: values.gateway_cpu_millicores,
			ram_gb: values.gateway_ram_gb,
			disk_gb: values.gateway_disk_gb,
		},
		storage: {
			cpu_millicores: values.storage_cpu_millicores,
			ram_gb: values.storage_ram_gb,
			disk_gb: values.storage_disk_gb,
		},
	};
}

function telemetryConfig(values) {
	return {
		telemetry: {
			cpu_millicores: values.datum_cpu_millicores,
			ram_gb: values.datum_ram_gb,
			disk_gb: values.datum_disk_gb,
		},
	};
}

function intField(fieldname, label, defaultValue) {
	return { fieldname, fieldtype: "Int", label, reqd: 1, default: defaultValue };
}

function showProvisionDialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Provision Cargo Server"),
		size: "large",
		fields: [
			{ fieldtype: "Section Break", label: __("Cargo Server") },
			{
				fieldname: "virtual_machine_image",
				fieldtype: "Link",
				label: __("Image"),
				options: "Virtual Machine Image",
				reqd: 1,
				filters: { enabled: 1, status: "Available", image_type: "system" },
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "public_ipv4",
				fieldtype: "Link",
				label: __("Public IPv4"),
				options: "Public IP Allocation",
				reqd: 1,
				filters: {
					status: "Reserved",
					version: "4",
					tenant_id: 0,
					virtual_machine: ["is", "not set"],
				},
			},
			{ fieldtype: "Section Break", label: __("Cargo Server Resources") },
			intField("cpu_millicores", __("CPU (millicores)"), 2000),
			{ fieldtype: "Column Break" },
			intField("memory_mib", __("Memory (MiB)"), 4096),
			{ fieldtype: "Column Break" },
			intField("disk_mib", __("Disk (MiB)"), 16384),

			{ fieldtype: "Section Break", label: __("Object Storage Cluster (Garage)") },
			intField("storage_node_count", __("Storage Nodes"), 3),
			{ fieldtype: "Column Break" },
			intField("replication_factor", __("Replication Factor"), 3),
			{ fieldtype: "Section Break", label: __("Garage Node Resources") },
			intField("gateway_cpu_millicores", __("Gateway CPU (millicores)"), 2000),
			intField("storage_cpu_millicores", __("Storage CPU (millicores)"), 4000),
			{ fieldtype: "Column Break" },
			intField("gateway_ram_gb", __("Gateway Memory (GB)"), 4),
			intField("storage_ram_gb", __("Storage Memory (GB)"), 8),
			{ fieldtype: "Column Break" },
			intField("gateway_disk_gb", __("Gateway Disk (GB)"), 20),
			intField("storage_disk_gb", __("Storage Disk (GB)"), 500),

			{ fieldtype: "Section Break", label: __("Datum (Telemetry Service)") },
			intField("datum_cpu_millicores", __("CPU (millicores)"), 2000),
			{ fieldtype: "Column Break" },
			intField("datum_ram_gb", __("Memory (GB)"), 4),
			{ fieldtype: "Column Break" },
			intField("datum_disk_gb", __("Disk (GB)"), 20),
		],
		primary_action_label: __("Provision"),
		primary_action(values) {
			if (values.replication_factor > values.storage_node_count) {
				frappe.throw(__("Replication factor cannot exceed the number of storage nodes."));
			}
			frm.call({
				method: "provision",
				doc: frm.doc,
				args: {
					request: {
						...values,
						storage_cluster: storageClusterConfig(values),
						telemetry: telemetryConfig(values),
					},
				},
				freeze: true,
				freeze_message: __("Creating Cargo Server..."),
			}).then(() => {
				dialog.hide();
				return frm.refresh();
			});
		},
	});
	dialog.show();
}

function showPilotAdminPassword(response) {
	const dialog = new frappe.ui.Dialog({
		title: __("New Pilot Admin Password"),
		fields: [
			{
				fieldname: "domain",
				fieldtype: "Data",
				label: __("Admin Panel"),
				read_only: 1,
				default: `https://${response.domain}`,
			},
			{
				fieldname: "password",
				fieldtype: "Data",
				label: __("Password"),
				read_only: 1,
				default: response.password,
			},
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Atlas shows this password once."
				)}</p>`,
			},
		],
		primary_action_label: __("Copy Password"),
		primary_action() {
			frappe.utils.copy_to_clipboard(response.password);
			dialog.hide();
		},
	});
	dialog.show();
}

function setPilotReleaseTracker(frm, method, enabled) {
	const action = enabled ? __("enable") : __("disable");
	frappe.confirm(__("{0} automatic Pilot image builds?", [action]), () =>
		frm
			.call({
				method,
				doc: frm.doc,
				freeze: true,
				freeze_message: enabled
					? __("Enabling automatic Pilot image builds...")
					: __("Disabling automatic Pilot image builds..."),
			})
			.then(() => frm.refresh())
	);
}

frappe.ui.form.on("Cargo Server", {
	refresh(frm) {
		frm.disable_save();
		if (!has_common(frappe.user_roles, ["System Manager"])) {
			return;
		}

		if (!frm.doc.virtual_machine && ["Not Provisioned", "Archived"].includes(frm.doc.status)) {
			frm.page.set_primary_action(__("Provision"), () => showProvisionDialog(frm));
		}

		if (frm.doc.status === "Active") {
			frm.add_custom_button(
				__("Reset Pilot Admin Password"),
				() =>
					frappe.confirm(
						__("Reset the Pilot administration password on the Cargo host?"),
						() =>
							frm
								.call({
									method: "reset_pilot_admin_password",
									doc: frm.doc,
									freeze: true,
									freeze_message: __("Resetting the Pilot admin password..."),
								})
								.then((response) => showPilotAdminPassword(response.message))
					),
				__("Actions")
			);

			frm.add_custom_button(
				frm.doc.auto_build_pilot_images
					? __("Disable Auto Build Pilot Images")
					: __("Enable Auto Build Pilot Images"),
				() =>
					setPilotReleaseTracker(
						frm,
						frm.doc.auto_build_pilot_images
							? "disable_pilot_release_tracker"
							: "enable_pilot_release_tracker",
						!frm.doc.auto_build_pilot_images
					),
				__("Actions")
			);
		}

		if (frm.doc.virtual_machine) {
			frm.add_custom_button(
				__("Archive"),
				() =>
					frappe.confirm(__("Archive the Cargo Server?"), () =>
						frm
							.call({ method: "archive", doc: frm.doc, freeze: true })
							.then(() => frm.refresh())
					),
				__("Dangerous Actions")
			);
		}
	},
});
