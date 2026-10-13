# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.metal_server.core.host_installation import HostInstallation, get_failure_reason
from atlas.vm.core.metal_client import MetalClient, MetalClientError

if TYPE_CHECKING:
	from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.operations.doctype.metald_update_server.metald_update_server import MetaldUpdateServer

SYNCHRONIZATION_LIMIT_MINUTES = 2
VERSION_READ_TIMEOUT_SECONDS = 120


class MetaldUpdate(Document):
	"""Move Metal Servers to one pinned metald and Atlas WG Mesh build in batches."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.operations.doctype.metald_update_server.metald_update_server import MetaldUpdateServer

		batch_size: DF.Int
		canary_size: DF.Int
		current_batch: DF.Int
		metald_binary_hash: DF.Data | None
		metald_binary_x86_64_file: DF.Link | None
		metald_version: DF.Data | None
		servers: DF.Table[MetaldUpdateServer]
		status: DF.Literal["Draft", "Planning", "Planned", "Running", "Paused", "Completed", "Cancelled"]
		wait_minutes: DF.Int
		wait_until: DF.Datetime | None
		wg_mesh_binary_hash: DF.Data | None
		wg_mesh_binary_x86_64_file: DF.Link | None
		wg_mesh_version: DF.Data | None
	# end: auto-generated types

	def before_insert(self) -> None:
		# A duplicate keeps its pinned build, so it can roll the servers back.
		if not self.metald_binary_x86_64_file:
			self.pin_latest_build()
		if not self.servers:
			for server in frappe.get_all(
				"Metal Server", filters={"status": "Running"}, pluck="name", order_by="title asc"
			):
				self.append("servers", {"server": server, "status": "Pending"})

	def validate(self) -> None:
		if not self.is_new() and self.get_doc_before_save().status != "Draft":
			frappe.throw(_("Only a Draft Metald Update can change."))
		if self.canary_size < 1 or self.batch_size < 1:
			frappe.throw(_("Canary Size and Batch Size must be at least 1."))

		servers = [row.server for row in self.servers]
		if not servers:
			frappe.throw(_("Add at least one running server."))
		if len(servers) != len(set(servers)):
			frappe.throw(_("Each server can be in the update only once."))

	def pin_latest_build(self) -> None:
		"""Copy the latest build from Atlas Settings."""
		settings = frappe.get_single("Atlas Settings")
		if not (
			settings.metald_version
			and settings.metald_binary_x86_64_file
			and settings.wg_mesh_binary_x86_64_file
		):
			frappe.throw(
				_("Atlas Settings has no versioned metald and Atlas WG Mesh build. Run build-metald.")
			)

		for field in (
			"metald_version",
			"metald_binary_x86_64_file",
			"metald_binary_hash",
			"wg_mesh_version",
			"wg_mesh_binary_x86_64_file",
			"wg_mesh_binary_hash",
		):
			self.set(field, settings.get(field))

	@frappe.whitelist(methods=["POST"])
	def plan(self) -> None:
		"""Start reading the versions of each server."""
		frappe.only_for("System Manager")
		self.lock_status("Draft")
		for row in self.servers:
			row.db_set({"status": "Planning", "status_reason": None, "batch": 0})
		self.db_set("status", "Planning")
		enqueue_next_step()

	@frappe.whitelist(methods=["POST"])
	def edit_plan(self) -> None:
		"""Return the update to Draft, so its servers and settings can change."""
		frappe.only_for("System Manager")
		self.lock_status("Planning", "Planned")
		self.db_set("status", "Draft")

	@frappe.whitelist(methods=["POST"])
	def start(self) -> None:
		"""Assign batches to the servers that need the build and start the rollout."""
		frappe.only_for("System Manager")
		# Lock every update that can start or run, in name order, so two starts cannot both pass.
		statuses = frappe.db.get_values(
			"Metald Update",
			{"status": ("in", ("Planned", "Running", "Paused"))},
			"status",
			order_by="name asc",
			for_update=True,
			pluck=True,
		)
		self.lock_status("Planned")
		if any(row.status == "Failed" for row in self.servers):
			frappe.throw(_("Some server versions could not be read. Edit the plan and plan again."))
		if "Running" in statuses or "Paused" in statuses:
			frappe.throw(_("Another Metald Update runs. Complete or cancel it first."))

		pending_rows = [row for row in self.servers if row.status == "Pending"]
		for index, row in enumerate(pending_rows):
			later_batch = (index - self.canary_size) // self.batch_size + 2
			row.db_set("batch", 1 if index < self.canary_size else later_batch)
		self.db_set({"status": "Running", "current_batch": 1})
		enqueue_next_step()

	@frappe.whitelist(methods=["POST"])
	def pause(self) -> None:
		"""Stop new upgrades. Running SSH Tasks finish."""
		frappe.only_for("System Manager")
		self.lock_status("Running")
		self.db_set("status", "Paused")

	@frappe.whitelist(methods=["POST"])
	def resume(self) -> None:
		"""Upgrade the failed servers again and continue."""
		frappe.only_for("System Manager")
		self.lock_status("Paused")
		for row in self.servers:
			if row.status == "Failed":
				row.db_set({"status": "Pending", "status_reason": None})
		self.db_set({"status": "Running", "wait_until": None})
		enqueue_next_step()

	@frappe.whitelist(methods=["POST"])
	def cancel(self) -> None:
		"""Stop the rollout for good. Running SSH Tasks finish."""
		frappe.only_for("System Manager")
		self.lock_status("Running", "Paused")
		self.db_set("status", "Cancelled")

	def lock(self) -> str:
		"""Lock and reload this update, and return its status."""
		status = frappe.db.get_value(self.doctype, self.name, "status", for_update=True)
		self.load_from_db()
		return status

	def lock_status(self, *allowed_statuses: str) -> None:
		"""Lock and reload this update, and check that its status allows the action."""
		if (status := self.lock()) not in allowed_statuses:
			frappe.throw(_("Metald Update {0} is {1}.").format(self.name, status))

	def run_next_step(self) -> None:
		"""Run one step of the plan or the rollout. A step only touches the database and queues jobs."""
		# A Desk action can change the status after the scheduler listed the update.
		status = self.lock()
		if status == "Planning":
			self.run_planning_step()
		elif status == "Running":
			self.run_rollout_step()

	def run_planning_step(self) -> None:
		"""Queue the version reads, then mark the update Planned, or Completed when no server needs the build."""
		planning_rows = [row for row in self.servers if row.status == "Planning"]
		if planning_rows:
			queue_version_reads(planning_rows)
		elif all(row.status == "Skipped" for row in self.servers):
			self.db_set("status", "Completed")
		else:
			self.db_set("status", "Planned")

	def run_rollout_step(self) -> None:
		"""Move the batches up to the current one."""
		# Earlier batches stay in scope, so a check also finds a host that failed after its own batch.
		rows = [row for row in self.servers if 0 < row.batch <= self.current_batch]
		self.collect_upgrades(rows)
		statuses = {row.status for row in rows}

		if "Upgrading" in statuses or "Checking" in statuses:
			queue_version_reads([row for row in rows if row.status == "Checking"])
		elif "Pending" in statuses:
			self.start_upgrades([row for row in rows if row.status == "Pending"])
		elif "Failed" in statuses:
			self.db_set("status", "Paused")
		elif "Upgraded" in statuses:
			self.check_after_wait(rows)
		elif self.current_batch == max(row.batch for row in self.servers):
			self.db_set({"status": "Completed", "wait_until": None})
		else:
			self.db_set({"current_batch": self.current_batch + 1, "wait_until": None})

	def start_upgrades(self, rows: list[MetaldUpdateServer]) -> None:
		"""Start one upgrade SSH Task for each running server and link it to the row."""
		for row in rows:
			server: MetalServer = frappe.get_doc("Metal Server", row.server)
			if server.status != "Running":
				row.db_set(
					{"status": "Skipped", "status_reason": _("The server is {0}.").format(server.status)}
				)
				continue
			# Another metald operation from Desk runs on this server.
			if is_job_enqueued(server.metald_job_id):
				continue

			task = HostInstallation(server).upgrade_metald(self, run_in_background=True)
			row.db_set({"status": "Upgrading", "ssh_task": task.name, "status_reason": None})

	def collect_upgrades(self, rows: list[MetaldUpdateServer]) -> None:
		"""Copy the result of each finished upgrade SSH Task into its row."""
		for row in rows:
			if row.status != "Upgrading":
				continue
			if not row.ssh_task:
				row.db_set({"status": "Failed", "status_reason": _("The upgrade has no SSH Task.")})
				continue

			task: SSHTask = frappe.get_doc("SSH Task", row.ssh_task)
			if task.status == "Success":
				row.db_set("status", "Upgraded")
			elif task.status == "Failed":
				row.db_set({"status": "Failed", "status_reason": get_failure_reason(task.output)})
			elif task.status == "Pending" and not is_job_enqueued(task.job_id):
				row.db_set({"status": "Failed", "status_reason": _("The SSH Task did not start.")})

	def check_after_wait(self, rows: list[MetaldUpdateServer]) -> None:
		"""Start the wait, and after it check every upgraded server again."""
		if not self.wait_until:
			self.db_set("wait_until", add_to_date(now_datetime(), minutes=self.wait_minutes))
		elif now_datetime() >= get_datetime(self.wait_until):
			checked_rows = [row for row in rows if row.status in ("Upgraded", "Verified")]
			for row in checked_rows:
				row.db_set({"status": "Checking", "status_reason": None})
			queue_version_reads(checked_rows)

	def get_version_problem(self, versions: dict[str, str | None]) -> str | None:
		"""Return why the versions on a server differ from the pinned build, or None."""
		for field, label in (("metald_version", "metald"), ("wg_mesh_version", "Atlas WG Mesh")):
			if not versions[field]:
				return _("The server does not report its {0} version.").format(label)
			if versions[field] != self.get(field):
				return _("The server runs {0} {1}, not {2}.").format(label, versions[field], self.get(field))
		return None

	def get_health_problem(self, server_name: str, versions: dict[str, str | None]) -> str | None:
		"""Return why an upgraded server is not healthy, or None."""
		if problem := self.get_version_problem(versions):
			return problem

		last_synchronization = frappe.db.get_value(
			"Metal Server Usage", {"server": server_name}, "creation", order_by="creation desc"
		)
		limit = add_to_date(now_datetime(), minutes=-SYNCHRONIZATION_LIMIT_MINUTES)
		if not last_synchronization or get_datetime(last_synchronization) < limit:
			return _("The server did not synchronize in the last {0} minutes.").format(
				SYNCHRONIZATION_LIMIT_MINUTES
			)
		return None


def get_host_versions(server: MetalServer) -> dict[str, str | None]:
	"""Return the versions that a server reports. A metald without the route reports none."""
	try:
		versions = MetalClient(server).get_versions()
	except MetalClientError as error:
		if not error.is_not_found:
			raise
		versions = {}
	return {
		field: versions.get(field) for field in ("metald_version", "wg_mesh_version", "firecracker_version")
	}


def enqueue_next_step() -> None:
	"""Queue the next step after the current transaction commits."""
	frappe.enqueue(
		run_metald_update_steps,
		queue="short",
		job_id="atlas||metald-update||step",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def queue_version_reads(rows: list[MetaldUpdateServer]) -> None:
	"""Queue one version read for each row."""
	for row in rows:
		job_id = f"atlas||metald-update||read-versions||{row.name}"
		if is_job_enqueued(job_id):
			continue
		frappe.enqueue(
			read_server_versions,
			queue="short",
			timeout=VERSION_READ_TIMEOUT_SECONDS,
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=True,
			update_name=row.parent,
			row_name=row.name,
		)


@run_as_admin
def run_metald_update_steps() -> None:
	"""Run one step of each Metald Update that plans or rolls out."""
	for name in frappe.get_all(
		"Metald Update", filters={"status": ("in", ("Planning", "Running"))}, pluck="name"
	):
		frappe.get_doc("Metald Update", name).run_next_step()


@run_as_admin
def read_server_versions(update_name: str, row_name: str) -> None:
	"""Read the versions of one server and record the planning or check result in its row."""
	metald_update: MetaldUpdate = frappe.get_doc("Metald Update", update_name)
	row = metald_update.get("servers", {"name": row_name})[0]
	if row.status not in ("Planning", "Checking"):
		return

	server: MetalServer = frappe.get_doc("Metal Server", row.server)
	if server.status != "Running":
		status = "Skipped" if row.status == "Planning" else "Failed"
		row.db_set({"status": status, "status_reason": _("The server is {0}.").format(server.status)})
		return
	try:
		versions = get_host_versions(server)
	except Exception as error:
		# A failed read must end in the row, or the step queues it again forever.
		if not isinstance(error, MetalClientError):
			frappe.log_error(f"Metald Update {update_name}: could not read the versions of {server.name}")
		row.db_set({"status": "Failed", "status_reason": str(error)})
		return

	if row.status == "Planning":
		problem = metald_update.get_version_problem(versions)
		status = "Pending" if problem else "Skipped"
		reason = None if problem else _("The server already runs the build.")
	else:
		reason = metald_update.get_health_problem(row.server, versions)
		status = "Failed" if reason else "Verified"
	row.db_set({**versions, "status": status, "status_reason": reason})


@frappe.whitelist(methods=["POST"])
def create_metald_update(servers: str) -> str:
	"""Create a Draft Metald Update for the selected Metal Servers."""
	frappe.only_for("System Manager")
	rows = [{"server": server} for server in frappe.parse_json(servers)]
	metald_update = frappe.get_doc({"doctype": "Metald Update", "servers": rows})
	return metald_update.insert().name
