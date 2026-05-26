import json
from typing import ClassVar

import frappe
from frappe.model.document import Document

from atlas.atlas import scripts_catalog
from atlas.atlas.ssh import connection_for_server, run_task, upload_files


class Server(Document):
	BOOTSTRAP_ALLOWED_STATUS: ClassVar[set[str]] = {"Pending", "Bootstrapping", "Active", "Broken"}
	BOOTSTRAP_UPLOAD_SOURCES: ClassVar[list[tuple[str, str]]] = [
		("vm-network-up.sh", "/var/lib/atlas/bin/vm-network-up.sh"),
		("vm-network-down.sh", "/var/lib/atlas/bin/vm-network-down.sh"),
		("systemd/firecracker-vm@.service", "/etc/systemd/system/firecracker-vm@.service"),
	]

	@frappe.whitelist()
	def bootstrap(self) -> str:
		"""Upload helpers + unit, run bootstrap-server.sh. Returns Task name."""
		if self.status not in self.BOOTSTRAP_ALLOWED_STATUS:
			frappe.throw(f"Cannot bootstrap from status {self.status}")

		upload_files(connection_for_server(self), self._bootstrap_uploads())

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

	def _bootstrap_uploads(self) -> list[tuple[str, str]]:
		directory = scripts_catalog.scripts_directory()
		return [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
		]

	def _absorb_bootstrap_output(self, stdout: str) -> None:
		# Script tail-prints /var/lib/atlas/bootstrap.json (compact, single
		# line) as the canonical source of truth. `set -x` writes to stderr,
		# so stdout is clean — the last non-empty line is the JSON object.
		last_line = next(
			(line for line in reversed(stdout.splitlines()) if line.strip()),
			"",
		)
		parsed = json.loads(last_line)
		self.firecracker_version = parsed["firecracker_version"]
		self.kernel_version = parsed["kernel_version"]
		self.architecture = parsed["architecture"]
