import json
import re

import frappe
from frappe.model.document import Document

from atlas.atlas import scripts_catalog
from atlas.atlas.ssh import run_task, upload_files

BOOTSTRAP_UPLOADS = [
	("vm-network-up.sh", "/var/lib/atlas/bin/vm-network-up.sh"),
	("vm-network-down.sh", "/var/lib/atlas/bin/vm-network-down.sh"),
	("systemd/firecracker-vm@.service", "/etc/systemd/system/firecracker-vm@.service"),
]

BOOTSTRAP_ALLOWED_STATUS = {"Pending", "Bootstrapping", "Active", "Broken"}

KEY_VALUE_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.+)$")


class Server(Document):
	@frappe.whitelist()
	def bootstrap(self) -> str:
		"""Upload helpers + unit, run bootstrap-server.sh. Returns Task name."""
		if self.status not in BOOTSTRAP_ALLOWED_STATUS:
			frappe.throw(f"Cannot bootstrap from status {self.status}")

		from atlas.atlas.ssh import connection_for_server  # noqa: PLC0415

		connection = connection_for_server(self)
		upload_files(connection, _resolved_uploads())

		task = run_task(
			server=self.name,
			script="bootstrap-server.sh",
			variables={
				"FIRECRACKER_VERSION": "v1.15.1",
				"ARCHITECTURE": "x86_64",
			},
		)
		self._absorb_bootstrap_output(task.stdout)
		self.save(ignore_permissions=True)
		return task.name

	@frappe.whitelist()
	def reboot(self) -> str:
		"""Run reboot-server.sh as a Task. SSH drops mid-Task — Task ends in
		Failure; the operator confirms reboot by waiting and reconnecting."""
		return self.run_task_dialog(script="reboot-server.sh", variables={})

	@frappe.whitelist()
	def run_task_dialog(self, script: str, variables: dict | str | None = None) -> str:
		"""Operator escape hatch. Same code path as bootstrap/provision.

		`variables` is a dict (JS form post) or JSON string. Returns Task name.
		"""
		if isinstance(variables, str):
			variables = json.loads(variables or "{}")
		if variables is None:
			variables = {}
		if not isinstance(variables, dict):
			frappe.throw("variables must be a JSON object")
		if script not in scripts_catalog.allowed_scripts():
			frappe.throw(f"Unknown script: {script}")
		task = run_task(
			server=self.name,
			script=script,
			variables=variables,
			timeout_seconds=1800,
		)
		return task.name

	@frappe.whitelist()
	def get_scripts(self) -> list[str]:
		"""Whitelisted: scripts available for Run Task dialog."""
		return scripts_catalog.allowed_scripts()

	def _absorb_bootstrap_output(self, stdout: str) -> None:
		fields = {"FIRECRACKER_VERSION": "firecracker_version",
		          "KERNEL_VERSION": "kernel_version",
		          "ARCHITECTURE": "architecture"}
		for line in stdout.splitlines():
			match = KEY_VALUE_LINE.match(line.strip())
			if not match:
				continue
			key, value = match.group(1), match.group(2).strip()
			fieldname = fields.get(key)
			if fieldname:
				setattr(self, fieldname, value)


def _resolved_uploads() -> list[tuple[str, str]]:
	from atlas.atlas.ssh import SCRIPTS_DIRECTORY  # noqa: PLC0415
	return [
		(str(SCRIPTS_DIRECTORY / source), destination)
		for source, destination in BOOTSTRAP_UPLOADS
	]
