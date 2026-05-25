import json
import re

import frappe
from frappe.model.document import Document

from atlas.atlas import scripts_catalog
from atlas.atlas.ssh import run_task, run_task_on_server, upload_files

BOOTSTRAP_UPLOADS = [
	("scripts/vm-network-up.sh", "/var/lib/atlas/bin/vm-network-up.sh"),
	("scripts/vm-network-down.sh", "/var/lib/atlas/bin/vm-network-down.sh"),
	(
		"scripts/systemd/firecracker-vm@.service",
		"/etc/systemd/system/firecracker-vm@.service",
	),
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
			connection=connection,
			script="bootstrap-server.sh",
			variables={
				"FIRECRACKER_VERSION": "v1.15.1",
				"ARCHITECTURE": "x86_64",
			},
			server=self.name,
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
		task = run_task_on_server(
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

	@frappe.whitelist()
	def get_form_extras(self) -> dict:
		"""Whitelisted: lists rendered into HTML areas on the form."""
		virtual_machines = frappe.get_all(
			"Virtual Machine",
			filters={"server": self.name},
			fields=["name", "description", "status", "vcpus",
			        "memory_megabytes", "ipv6_address"],
			order_by="creation desc",
			limit=50,
		)
		recent_tasks = frappe.get_all(
			"Task",
			filters={"server": self.name},
			fields=["name", "script", "status", "duration_milliseconds",
			        "creation"],
			order_by="creation desc",
			limit=10,
		)
		return {
			"virtual_machines": virtual_machines,
			"recent_tasks": recent_tasks,
		}

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
	resolved = []
	for local, remote in BOOTSTRAP_UPLOADS:
		# `local` is relative to the repo root; SCRIPTS_DIRECTORY ends in /scripts,
		# so strip the leading "scripts/" and re-join.
		assert local.startswith("scripts/"), local
		local_path = SCRIPTS_DIRECTORY / local[len("scripts/"):]
		resolved.append((str(local_path), remote))
	return resolved
