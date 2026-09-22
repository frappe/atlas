from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import timestamp_field
from atlas.vm.core.models import VirtualMachineShape
from atlas.vm.core.placement import PlacementRequirements, PlacementStrategy
from atlas.vm.core.placement.transaction import use_read_committed
from atlas.vm.core.vm_state import LIVE_STATES

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_migration.virtual_machine_migration import (
		VirtualMachineMigration,
	)

COPY_POLL_SECONDS = 5
TRANSITION_POLL_SECONDS = 2
TRANSITION_PHASES = frozenset({"stopping", "starting"})
MAXIMUM_RUN_DURATION = timedelta(minutes=30)
MIGRATION_JOB_TIMEOUT_SECONDS = int(MAXIMUM_RUN_DURATION.total_seconds()) + 120
DESTINATION_VISIBILITY_TIMEOUT = timedelta(minutes=10)
TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted"})


class MigrationService:
	"""Own Atlas orchestration and persisted state for one VM migration."""

	def __init__(self, migration: VirtualMachineMigration) -> None:
		self.migration = migration

	@classmethod
	@use_read_committed
	def create(
		cls,
		virtual_machine: VirtualMachine,
		destination_metal_server: str | None = None,
		resize: VirtualMachineShape | None = None,
	) -> str:
		"""Create a scheduled migration. A resize gives the VM a new shape on the destination."""
		locked = cast(
			"VirtualMachine", frappe.get_doc("Virtual Machine", virtual_machine.name, for_update=True)
		)
		locked.check_permission("write")
		cls.validate_source(locked)
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": locked.name,
				"source_metal_server": locked.server,
				"destination_metal_server": destination_metal_server,
			}
		)
		if resize:
			migration.target_cpu_millicores = resize.cpu_millicores
			migration.target_memory_mib = resize.memory_mib
			migration.target_disk_mib = resize.disk_mib
		migration.flags.created_by_vm_migration_action = True
		migration.insert(ignore_permissions=True)
		return cast(str, migration.name)

	@classmethod
	@use_read_committed
	def schedule(cls, migration: VirtualMachineMigration) -> None:
		"""Claim a VM and initialize a newly inserted scheduled migration."""
		virtual_machine = cast(
			"VirtualMachine",
			frappe.get_doc("Virtual Machine", migration.virtual_machine, for_update=True),
		)
		virtual_machine.check_permission("write")
		cls.validate_source(virtual_machine)

		scheduled_at = now_datetime()
		migration.db_set("source_metal_server", virtual_machine.server)
		migration.db_set("status", "scheduled")
		migration.db_set("scheduled_at", scheduled_at)
		virtual_machine.db_set("active_migration", migration.name)

	@staticmethod
	def validate_source(virtual_machine: VirtualMachine) -> None:
		"""Reject a VM that cannot start a migration."""
		if virtual_machine.active_migration:
			frappe.throw(
				_("Virtual Machine {0} is already migrating.").format(virtual_machine.name),
				exc=AtlasUserError,
			)
		if virtual_machine.is_draft or virtual_machine.is_terminating:
			frappe.throw(
				_("Virtual Machine {0} is not ready to migrate.").format(virtual_machine.name),
				exc=AtlasUserError,
			)
		if virtual_machine.current_state not in LIVE_STATES:
			frappe.throw(
				_("Virtual Machine {0} must be running, stopped, or paused to migrate.").format(
					virtual_machine.name
				),
				exc=AtlasUserError,
			)

	def destination_shape(self, virtual_machine: VirtualMachine) -> VirtualMachineShape:
		"""Return the shape the VM has on the destination."""
		if self.has_target_shape:
			return VirtualMachineShape(
				self.migration.target_cpu_millicores,
				self.migration.target_memory_mib,
				self.migration.target_disk_mib,
				virtual_machine.sleep_after_idle_seconds,
			)
		return VirtualMachineShape(
			virtual_machine.cpu_millicores,
			virtual_machine.memory_mib,
			virtual_machine.disk_mib,
			virtual_machine.sleep_after_idle_seconds,
		)

	def run(self) -> None:
		"""Select a destination when needed, then drive it until it settles."""
		if self.migration.status in TERMINAL_STATUSES:
			return
		if self.is_canceling and not self.migration.destination_metal_server:
			self.settle("aborted")
			return
		if self.migration.status == "scheduled":
			if not self.select_destination_metal_server():
				return

		if self.is_canceling:
			self.request_destination_abort()
		else:
			self.send_request()

		deadline = now_datetime() + MAXIMUM_RUN_DURATION
		while now_datetime() < deadline:
			status = self.poll()
			if self.advance(status):
				return
			time.sleep(self.poll_interval(status))

		self.fail("migration_timeout", f"The migration did not settle within {MAXIMUM_RUN_DURATION}.")
		self.request_destination_abort()

	def select_destination_metal_server(self) -> bool:
		"""Reserve the requested or an eligible destination. Return False to retry later."""
		virtual_machine = cast(
			"VirtualMachine", frappe.get_doc("Virtual Machine", self.migration.virtual_machine)
		)
		shape = self.destination_shape(virtual_machine)
		requirements = PlacementRequirements(
			shape.cpu_millicores,
			shape.memory_mib,
			shape.disk_mib,
			cast(str, virtual_machine.architecture),
			virtual_machine.tenant_id,
			shape.sleep_after_idle_seconds > 0,
		)
		requested_destination = self.migration.destination_metal_server

		try:
			if requested_destination:
				if requested_destination == self.migration.source_metal_server:
					raise AtlasUserError(
						_("Choose a destination Metal Server other than {0}.").format(requested_destination)
					)
				destination = PlacementStrategy.reserve_server(
					requirements,
					requested_destination,
					exclude_servers={self.migration.source_metal_server},
				)
			else:
				destination = PlacementStrategy.find_server(
					requirements, exclude_servers={self.migration.source_metal_server}
				)
		except AtlasUserError as error:
			self.record_destination_metal_server_selection_failure(error)
			if requested_destination:
				self.update(
					{
						"error_code": "destination_unavailable",
						"error_message": str(error),
						"error_at": now_datetime(),
					}
				)
				self.settle("failed")
			return False

		started_at = now_datetime()
		self.update(
			{
				"destination_metal_server": destination,
				"status": "preparing",
				"started_at": started_at,
				"destination_metal_server_selection_message": None,
				"progress_percent": 5,
			}
		)
		return True

	def record_destination_metal_server_selection_failure(self, error: Exception) -> None:
		"""Record one unsuccessful destination selection attempt."""
		now = now_datetime()
		self.update(
			{
				"destination_metal_server_selection_attempts": int(
					self.migration.destination_metal_server_selection_attempts or 0
				)
				+ 1,
				"last_destination_metal_server_selection_at": now,
				"destination_metal_server_selection_message": str(error),
			}
		)

	def advance(self, status: dict[str, Any]) -> bool:
		"""Apply one Metal response. Return True after a terminal transition."""
		state = status.get("status")
		if state == "completed":
			self.settle("completed")
			return True
		if state == "aborted":
			self.settle("failed" if self.migration.error_code else "aborted")
			return True
		if state == "failed":
			error = status.get("error") or {}
			self.fail(
				str(error.get("code") or "migration_error"),
				str(error.get("message") or "Metal failed migration."),
			)
			self.request_destination_abort()
			if self.is_expired:
				self.settle("failed")
				return True
			return False
		if state == "missing":
			return self.handle_missing_destination()
		if self.is_canceling:
			self.request_destination_abort()
			return False
		if state == "ready":
			self.commit_destination()
			self.finish()
		return False

	def handle_missing_destination(self) -> bool:
		"""Retry an invisible destination, or fail it after the visibility timeout."""
		if self.is_expired:
			self.fail(
				"destination_unreachable",
				"The destination did not answer before the visibility timeout.",
			)
			self.request_destination_abort()
			return False
		try:
			self.send_request()
		except MetalClientError as error:
			self.record_error(error)
		return False

	def send_request(self) -> None:
		"""Send the repeatable destination-pull request."""
		source_coordination_url = MetalClient.get_coordination_url(self.source_metal_server)
		resize = None
		if self.has_target_shape:
			virtual_machine = cast(
				"VirtualMachine", frappe.get_doc("Virtual Machine", self.migration.virtual_machine)
			)
			resize = self.destination_shape(virtual_machine).migration_resize
		self.destination_client.put_migration(
			self.migration.name, self.migration.virtual_machine, source_coordination_url, resize
		)

	def poll(self) -> dict[str, Any]:
		"""Read Metal state and store its typed progress values."""
		try:
			status = self.destination_client.get_migration(self.migration.name)
		except MetalClientError as error:
			if error.is_not_found or error.retryable:
				if error.retryable:
					self.record_error(error)
				return {"status": "missing"}
			raise
		self.store_progress(status)
		return status

	@staticmethod
	def poll_interval(status: dict[str, Any]) -> int:
		"""Return the wait time for one Metal migration state."""
		return TRANSITION_POLL_SECONDS if status.get("phase") in TRANSITION_PHASES else COPY_POLL_SECONDS

	def store_progress(self, response: dict[str, Any]) -> None:
		"""Store one Metal response as typed parent fields and transfer rows."""
		transfers = response.get("transfers") or []
		status = self.status_from_response(response)
		values: dict[str, Any] = {
			"duration_seconds": self.duration_seconds,
		}
		if status:
			values["status"] = status
			values["progress_percent"] = self.progress_percent(status, transfers)
		self.update(values, transfers=transfers, commit=True)

	def status_from_response(self, response: dict[str, Any]) -> str | None:
		"""Map Metal runtime phases to the Atlas lifecycle status."""
		if response.get("status") == "ready":
			return "finalizing"
		if response.get("status") != "running":
			return None
		return {
			"preparing": "preparing",
			"copying": "copying",
			"stopping": "cutting_over",
			"starting": "starting",
			"finishing": "finalizing",
			"rollback": "canceling",
		}.get(response.get("phase"), "preparing")

	def progress_percent(self, status: str, transfers: list[dict[str, Any]]) -> int:
		"""Return the documented lifecycle-progress estimate."""
		if status == "preparing":
			return 5
		if status == "copying":
			completed = sum(1 for transfer in transfers if transfer.get("completed"))
			current = next(
				(transfer for transfer in reversed(transfers) if not transfer.get("completed")), None
			)
			fraction = 0.0
			if current and current.get("total_mib"):
				fraction = min(1.0, current.get("transferred_mib", 0) / current["total_mib"])
			return min(70, int(10 + 60 * min(1.0, (completed + fraction) / 16)))
		return {"cutting_over": 75, "starting": 90, "finalizing": 95}.get(
			status, int(self.migration.progress_percent or 0)
		)

	def commit_destination(self) -> None:
		"""Commit the VM server, a resized shape, and finalizing status in one transaction."""
		virtual_machine = cast(
			"VirtualMachine",
			frappe.get_doc("Virtual Machine", self.migration.virtual_machine, for_update=True),
		)
		migration = cast(
			"VirtualMachineMigration",
			frappe.get_doc("Virtual Machine Migration", self.migration.name, for_update=True),
		)
		if virtual_machine.server != migration.destination_metal_server:
			virtual_machine.db_set("server", migration.destination_metal_server)
		if migration.target_memory_mib:
			virtual_machine.db_set(
				{
					"cpu_millicores": migration.target_cpu_millicores,
					"memory_mib": migration.target_memory_mib,
					"disk_mib": migration.target_disk_mib,
				}
			)
		migration.db_set("status", "finalizing")
		migration.db_set("progress_percent", 95)
		frappe.db.commit()  # nosemgrep
		self.migration = migration

	def finish(self) -> None:
		"""Tell the destination that Atlas committed the VM."""
		self.destination_client.finish_migration(self.migration.name)

	def request_abort(self) -> None:
		"""Cancel a scheduled migration or request cleanup from its destination."""
		if self.migration.status == "scheduled":
			self.settle("aborted")
			return

		if not self.is_canceling:
			self.update({"status": "canceling"}, commit=True)
		if self.migration.destination_metal_server:
			self.request_destination_abort()

	def request_destination_abort(self) -> None:
		"""Ask Metal to clean up a selected destination."""
		try:
			self.destination_client.abort_migration(self.migration.name)
		except MetalClientError as error:
			self.record_error(error)

	def fail(self, code: str, message: str) -> None:
		"""Record an error and continue destination cleanup."""
		self.update(
			{
				"status": "canceling",
				"error_code": code,
				"error_message": message,
				"error_at": now_datetime(),
			},
			commit=True,
		)

	def settle(self, status: str) -> None:
		"""Record a terminal result and release the VM action lock."""
		finished_at = now_datetime()
		values = {
			"status": status,
			"finished_at": finished_at,
			"duration_seconds": self.duration_seconds_at(finished_at),
		}
		if status == "completed":
			values["progress_percent"] = 100
		self.update(values)
		frappe.db.set_value("Virtual Machine", self.migration.virtual_machine, "active_migration", None)
		frappe.db.commit()  # nosemgrep

	def record_error(self, error: Exception) -> None:
		"""Store a nonterminal client error for operators."""
		self.update(
			{
				"error_code": "metal_client_error",
				"error_message": str(error),
				"error_at": now_datetime(),
			},
			commit=True,
		)

	def update(
		self, values: dict[str, Any], *, transfers: list[dict[str, Any]] | None = None, commit: bool = False
	) -> None:
		"""Write fresh migration fields and upsert typed transfer rows."""
		migration = cast(
			"VirtualMachineMigration", frappe.get_doc("Virtual Machine Migration", self.migration.name)
		)
		for fieldname, value in values.items():
			setattr(migration, fieldname, value)
		if transfers is not None:
			self.upsert_transfers(migration, transfers)
		migration.flags.updated_by_vm_migration_worker = True
		migration.save(ignore_permissions=True)
		if commit:
			frappe.db.commit()  # nosemgrep
		self.migration = migration

	@staticmethod
	def upsert_transfers(migration: VirtualMachineMigration, transfers: list[dict[str, Any]]) -> None:
		"""Copy Metal transfer values into their matching child rows."""
		rows = {int(row.idx): row for row in migration.transfers}
		for index, transfer in enumerate(transfers, start=1):
			sequence = int(transfer.get("sequence") or index)
			row = rows.get(sequence)
			if row is None:
				row = migration.append("transfers", {"idx": sequence})
			row.started_at = timestamp_field(transfer, "started_at")
			row.finished_at = timestamp_field(transfer, "finished_at")
			row.duration_seconds = transfer.get("duration_seconds", 0)
			row.transferred_mib = transfer.get("transferred_mib", 0)
			row.total_mib = transfer.get("total_mib", 0)
			row.throughput_mibps = transfer.get("throughput_mibps", 0)
			row.completed = transfer.get("completed", False)

	@property
	def has_target_shape(self) -> bool:
		"""Report whether the destination gives the VM a new shape."""
		return bool(self.migration.target_memory_mib)

	@property
	def is_canceling(self) -> bool:
		"""Return whether an operator or error requested cleanup."""
		return self.migration.status == "canceling"

	@property
	def is_expired(self) -> bool:
		"""Report whether a selected destination exceeded the visibility timeout."""
		return bool(
			self.migration.started_at
			and now_datetime() > get_datetime(self.migration.started_at) + DESTINATION_VISIBILITY_TIMEOUT
		)

	@property
	def duration_seconds(self) -> int:
		"""Return elapsed seconds after a destination reservation."""
		return self.duration_seconds_at(now_datetime())

	def duration_seconds_at(self, end: object) -> int:
		"""Return elapsed seconds until one timestamp."""
		if not self.migration.started_at:
			return 0
		return max(0, int((get_datetime(end) - get_datetime(self.migration.started_at)).total_seconds()))

	@property
	def destination_client(self) -> MetalClient:
		"""Return a Metal client for the selected destination."""
		return MetalClient(
			cast("MetalServer", frappe.get_doc("Metal Server", self.migration.destination_metal_server))
		)

	@property
	def source_metal_server(self) -> MetalServer:
		"""Return the source Metal Server record."""
		return cast("MetalServer", frappe.get_doc("Metal Server", self.migration.source_metal_server))


def reconcile_migration(migration_name: str) -> None:
	"""Queue the worker that advances one migration. A second call is deduplicated."""
	frappe.enqueue(
		"atlas.vm.core.vm_migration.run_migration",
		queue="long",
		timeout=MIGRATION_JOB_TIMEOUT_SECONDS,
		migration_name=migration_name,
		job_id=f"atlas||vm-migration||{migration_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


@run_as_admin
def run_migration(migration_name: str) -> None:
	"""Advance one migration until it completes or waits for destination capacity."""
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
	migration = cast("VirtualMachineMigration", frappe.get_doc("Virtual Machine Migration", migration_name))
	service = MigrationService(migration)
	try:
		service.run()
	except Exception as error:
		service.fail("migration_worker_error", str(error))
		frappe.log_error(
			title=f"Virtual Machine migration {migration_name} failed", message=frappe.get_traceback()
		)


@run_as_admin
def reconcile_migrations() -> None:
	"""Requeue every migration that still owns a VM action lock."""
	names = frappe.get_all(
		"Virtual Machine", filters={"active_migration": ["is", "set"]}, pluck="active_migration"
	)
	for name in names:
		reconcile_migration(name)
