"""Generic guest-SSH helpers — recording a guest operation as a Task row, a
remote-path parent, and the region-file path. Core substrate (Task rows + guest
paths), used by both the image machinery (core) and the proxy/tcp/deploy/gateway
routing code (services), so it lives in `core` and services imports it — never
the reverse.

Extracted from proxy.py, where they had accreted but were never proxy logic:
`_remote_parent` and `REGION_FILE` were not even used by proxy.py itself, and
`_record_guest_task` is a plain Task-row writer. Homing them here removes the
three core→services import edges (image_builder / image_recipes / image_build
were reaching into proxy for them).
"""

from __future__ import annotations

import frappe

# The guest file build.sh leaves empty and the proxy recipe's finalize step writes
# the real region into (image_recipes._finalize_proxy); init_by_lua reads it.
REGION_FILE = "/var/lib/nginx/region"


def _remote_parent(remote_path: str) -> str:
	parent = remote_path.rsplit("/", 1)[0]
	return parent or "/"


def _record_guest_task(
	virtual_machine: str, script: str, variables: dict, stdout: str, stderr: str, exit_code: int
) -> str:
	"""Record one guest-SSH operation as a Task row for the operator's audit
	trail. Unlike host Tasks this isn't a staged script — the `script` is a
	synthetic name and there are no uploads — but the row shape (status, output,
	exit code) is identical, so the operator sees proxy reconciles in the same
	Task list as every other action. Returns the Task's name so a caller (the
	Image Build controller) can link it for the audit trail."""
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"server": frappe.db.get_value("Virtual Machine", virtual_machine, "server"),
			"virtual_machine": virtual_machine,
			"script": script,
			"status": "Success" if exit_code == 0 else "Failure",
			"triggered_by": frappe.session.user if frappe.session else "Administrator",
			"stdout": stdout,
			"stderr": stderr,
			"exit_code": exit_code,
			"ended": frappe.utils.now_datetime(),
		}
	)
	task.variables_dict = variables
	task.insert(ignore_permissions=True)
	return task.name
