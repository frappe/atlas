from dataclasses import asdict

import frappe

from atlas.vm.core.metal_models import MetalHostCompatibility


def execute() -> None:
	"""Move existing host compatibility facts into Metal Server records."""
	for server in frappe.get_all("Metal Server", pluck="name"):
		key = f"metal-host-compatibility:{server}"
		report = frappe.cache().get_value(key, expires=True, use_local_cache=False)
		if isinstance(report, MetalHostCompatibility):
			frappe.db.set_value(
				"Metal Server",
				server,
				{
					"cpu_compatibility_report": frappe.as_json(asdict(report)),
					"tsc_scaling_supported": report.tsc_scaling,
				},
			)
		frappe.cache().delete_value(key)
