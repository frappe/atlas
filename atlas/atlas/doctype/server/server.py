import json
import uuid
from typing import ClassVar

import frappe
from frappe.model.document import Document

from atlas.atlas import scripts_catalog
from atlas.atlas.ssh import connection_for_server, run_task, upload_files
from atlas.atlas.task_results import parse_result

IMMUTABLE_AFTER_INSERT = (
	"title",
	"provider",
	"provider_resource_id",
	"size",
	"image",
	"ipv4_address",
	"ipv6_address",
	"ipv6_prefix",
	"ipv6_virtual_machine_range",
)


class Server(Document):
	BOOTSTRAP_ALLOWED_STATUS: ClassVar[set[str]] = {"Pending", "Bootstrapping", "Active", "Broken"}
	# Durable uploads beyond the atlas package (which _bootstrap_uploads()
	# computes from disk). The systemd-invoked hooks are .py now (positional
	# uuid); they and atlas-pool.service import the durable package under
	# /var/lib/atlas/bin (their sys.path shim adds that dir). The package itself
	# replaces the old durable lvm.sh — there is no shell helper library anymore.
	BOOTSTRAP_UPLOAD_SOURCES: ClassVar[list[tuple[str, str]]] = [
		("vm-network-up.py", "/var/lib/atlas/bin/vm-network-up.py"),
		("vm-network-down.py", "/var/lib/atlas/bin/vm-network-down.py"),
		# vm-disk-up.py re-activates the VM's thin-snapshot disk LV and refreshes
		# its in-jail block node at every unit start — the disk analogue of
		# vm-network-up.py, so an enabled VM self-heals its disk after a reboot.
		("vm-disk-up.py", "/var/lib/atlas/bin/vm-disk-up.py"),
		("systemd/firecracker-vm@.service", "/etc/systemd/system/firecracker-vm@.service"),
		("systemd/atlas-pool.service", "/etc/systemd/system/atlas-pool.service"),
	]

	def autoname(self) -> None:
		# UUID identity: title is the human label, name is opaque.
		self.name = str(uuid.uuid4())

	def validate(self) -> None:
		self._validate_immutability()

	def _validate_immutability(self) -> None:
		"""Lock fields once they carry a value. Allow None → value transitions
		so the DigitalOcean provision flow (`finish_provisioning`) can write
		IPv4/6 onto a freshly-inserted Pending row whose addresses weren't
		known at insert time."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			new_value = getattr(self, field)
			if not old_value:
				continue  # initial population is allowed
			if old_value != new_value:
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def archive(self) -> None:
		"""Destroy the vendor resource (idempotent), then mark Archived."""
		import atlas

		if self.status == "Archived":
			frappe.throw("Server is already archived")
		if self.provider_resource_id:
			atlas.get_provider().destroy(self.provider_resource_id)
		frappe.db.set_value(self.doctype, self.name, "status", "Archived")

	@frappe.whitelist()
	def sync_image(self, image: str) -> str:
		"""Single-server convenience wrapper around `Virtual Machine Image.sync_to_server`."""
		image_doc = frappe.get_doc("Virtual Machine Image", image)
		return image_doc.sync_to_server(self.name)

	@frappe.whitelist()
	def bootstrap(self) -> str:
		"""Upload helpers + unit, run bootstrap-server.py. Returns Task name."""
		if self.status not in self.BOOTSTRAP_ALLOWED_STATUS:
			frappe.throw(f"Cannot bootstrap from status {self.status}")

		upload_files(connection_for_server(self), self._bootstrap_uploads())

		task = run_task(
			server=self.name,
			script="bootstrap-server.py",
			variables={
				"FIRECRACKER_VERSION": "v1.15.1",
				"ARCHITECTURE": "x86_64",
			},
		)
		self._absorb_bootstrap_output(task.stdout)
		self.save(ignore_permissions=True)
		return task.name

	@frappe.whitelist()
	def sync_scripts(self) -> int:
		"""Re-upload the durable scripts (atlas package + systemd-invoked .py
		hooks) to /var/lib/atlas/bin without re-running bootstrap.

		The development fast path: after editing anything under scripts/lib/atlas/
		(or vm-network-up.py et al.) push the change to a live host in one scp
		sweep, instead of a full `bootstrap` (which also runs bootstrap-server.py
		and mutates status). Bootstrap remains the single refresh point for unit
		files; this is the subset that's pure code. Idempotent — a plain overwrite.

		Returns the number of files uploaded.
		"""
		if not self.ipv4_address:
			frappe.throw(f"Server {self.name} has no ipv4_address; cannot sync scripts")
		uploads = self._script_uploads()
		upload_files(connection_for_server(self), uploads)
		return len(uploads)

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
			try:
				variables = json.loads(variables or "{}")
			except json.JSONDecodeError as exception:
				frappe.throw(f"variables must be valid JSON: {exception}")
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
	def get_scripts(self) -> list[dict]:
		"""Whitelisted: operator-visible scripts + Run Task dialog metadata.

		Each entry is `{name, intro, fields}`. The client renders the dialog
		straight from this shape — fields are Frappe Dialog field dicts.

		The picker is intentionally shorter than `allowed_scripts()`.
		Lifecycle scripts (provision-vm, terminate-vm, vm-network-up, ...) are
		invoked from VM/Image controllers, not by hand from this dialog.
		"""
		return [
			{"name": name, **scripts_catalog.script_form(name)}
			for name in scripts_catalog.operator_visible_scripts()
		]

	def _bootstrap_uploads(self) -> list[tuple[str, str]]:
		return self._script_uploads() + self._unit_uploads()

	def _script_uploads(self) -> list[tuple[str, str]]:
		"""The durable scripts that live under /var/lib/atlas/bin: the importable
		atlas package plus the systemd-invoked .py hooks. These are pure code — an
		scp overwrite is all it takes for an edit to land, no daemon-reload. This
		is exactly the set `sync_scripts()` refreshes during development; bootstrap
		ships it alongside `_unit_uploads()`."""
		directory = scripts_catalog.scripts_directory()
		uploads = [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
			if destination.startswith("/var/lib/atlas/bin/")
		]
		# The durable atlas package: every lib module lands under
		# /var/lib/atlas/bin/atlas/ so the .py hooks and atlas-pool.service can
		# `import atlas`. Computed from disk (test_*.py skipped) so a new module
		# is shipped with no edit here — mirrors script_uploads.package staging.
		package_dir = directory / "lib" / "atlas"
		for entry in sorted(package_dir.glob("*.py")):
			if entry.name.startswith("test_"):
				continue
			uploads.append((str(entry), f"/var/lib/atlas/bin/atlas/{entry.name}"))
		return uploads

	def _unit_uploads(self) -> list[tuple[str, str]]:
		"""The bootstrap-only uploads that are NOT plain /var/lib/atlas/bin code —
		systemd unit files under /etc/systemd/system. Editing one needs a
		daemon-reload (a bootstrap concern), so `sync_scripts()` deliberately omits
		these."""
		directory = scripts_catalog.scripts_directory()
		return [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
			if not destination.startswith("/var/lib/atlas/bin/")
		]

	def _absorb_bootstrap_output(self, stdout: str) -> None:
		# bootstrap-server.py emits a typed BootstrapResult as one
		# `ATLAS_RESULT=<json>` line; parse_result pulls it out (the host still
		# also writes /var/lib/atlas/bootstrap.json as the on-disk source of
		# truth). Replaces the old "last non-empty stdout line is the JSON" scrape.
		parsed = parse_result(stdout)
		self.firecracker_version = parsed["firecracker_version"]
		self.jailer_version = parsed["jailer_version"]
		self.kernel_version = parsed["kernel_version"]
		self.architecture = parsed["architecture"]


def sync_scripts_to_all() -> dict[str, int]:
	"""Push the durable scripts to every Active server in one sweep.

	The development convenience: edit a script under scripts/lib/atlas/ once, then
	`bench --site <site> execute atlas.sync_scripts_to_all` (or `atlas.sync_scripts_to_all()`
	in a console) to refresh every live host. Active-only because a Pending/Broken
	server has no working SSH endpoint. Returns {server_name: files_uploaded}.
	"""
	results: dict[str, int] = {}
	for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
		server = frappe.get_doc("Server", name)
		results[name] = server.sync_scripts()
	return results
