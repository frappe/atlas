from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from rq.timeouts import JobTimeoutException

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.atlas.core.telemetry.events import count, observe
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalHostCompatibility, timestamp_field
from atlas.vm.core.models import VirtualMachineShape
from atlas.vm.core.placement import AffinityRules, PlacementRequirements, PlacementStrategy
from atlas.vm.core.placement.transaction import use_read_committed
from atlas.vm.core.vm_sleep import VirtualMachineSleepService
from atlas.vm.core.vm_state import LIVE_STATES

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.operations.doctype.metal_server_drain.metal_server_drain import MetalServerDrain
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
# A copy slower than this throughput, plus the margin, is stuck. The margin covers Metal's
# 30-minute incremental copy budget.
MINIMUM_COPY_THROUGHPUT_MIBPS = 25
COPY_TIMEOUT_MARGIN = timedelta(hours=1)
COPY_STATUSES = frozenset({"preparing", "copying"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted"})
# An operator restores the source only after the destination host stayed silent this long.
DESTINATION_LOST_AFTER = timedelta(minutes=15)
DESTINATION_LOST_CODE = "destination_lost"
# A lost destination that returns within this time still gets its migration discarded.
LOST_DESTINATION_DISCARD_WINDOW = timedelta(days=30)


class MigrationService:
	"""Own Atlas orchestration and persisted state for one VM migration."""

	def __init__(self, migration: VirtualMachineMigration) -> None:
		self.migration = migration
		self.last_contact_at: datetime | None = None

	@classmethod
	@use_read_committed
	def create(
		cls,
		virtual_machine: VirtualMachine,
		destination_metal_server: str | None = None,
		resize: VirtualMachineShape | None = None,
		has_memory_snapshot: bool = False,
		allow_disk_only_fallback: bool = True,
		metal_server_drain: str | None = None,
	) -> str:
		"""Create a scheduled migration. A resize gives the VM a new shape on the destination."""
		if is_vm_migration_paused_for_create():
			frappe.throw(_("VM migrations are paused."), exc=AtlasUserError)

		if virtual_machine.is_in_rescue_mode or virtual_machine.is_rescue_mode_enabled:
			frappe.throw(
				_("Disable rescue mode and restart the Virtual Machine first."), exc=AtlasConflictError
			)

		if virtual_machine.current_state not in LIVE_STATES:
			frappe.throw(
				_("Virtual Machine {0} must be running, stopped, or paused to migrate.").format(
					virtual_machine.name
				),
				exc=AtlasUserError,
			)
		locked: VirtualMachine = frappe.get_doc("Virtual Machine", virtual_machine.name, for_update=True)
		locked.check_permission("write")
		cls.validate_source(locked)
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": locked.name,
				"source_metal_server": locked.server,
				"destination_metal_server": destination_metal_server,
				"has_memory_snapshot": has_memory_snapshot,
				"allow_disk_only_fallback": allow_disk_only_fallback,
				"metal_server_drain": metal_server_drain,
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
		virtual_machine: VirtualMachine = frappe.get_doc(
			"Virtual Machine", migration.virtual_machine, for_update=True
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
		if frappe.db.exists(
			"Public IP Allocation",
			{"virtual_machine": virtual_machine.name, "status": ["in", ["Attaching", "Detaching"]]},
		):
			frappe.throw(
				_("The public address of Virtual Machine {0} is changing. Try again later.").format(
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
			# A status edited by hand skips settle, so release a lock that it left on the VM.
			self.release_virtual_machine()
			frappe.db.commit()  # nosemgrep
			return
		if self.is_canceling and not self.migration.destination_metal_server:
			self.settle("failed" if self.has_failed else "aborted")
			return
		if self.migration.status == "scheduled":
			if not self.select_destination_metal_server():
				return
			frappe.db.commit()  # nosemgrep

		# Metal bounds the copy and the cutover. At the deadline the reconciler starts a new run.
		deadline = now_datetime() + MAXIMUM_RUN_DURATION
		self.last_contact_at = now_datetime()
		self.request_destination()
		self.poll_until_settled(deadline)

	def request_destination(self) -> None:
		"""Ask the destination to continue or to abort. The poll loop retries a retryable error."""
		if self.is_canceling:
			self.request_destination_abort()
			return
		try:
			self.send_request()
		except MetalClientError as error:
			if not error.retryable:
				raise
			self.record_error(error)

	def poll_until_settled(self, deadline: datetime) -> None:
		"""Poll Metal until the migration settles, and fail a copy that outlives its limit."""
		copy_deadline = self.copy_deadline()
		while now_datetime() < deadline:
			status = self.poll()
			if self.advance(status):
				return
			is_copying = self.status_from_response(status) in COPY_STATUSES
			if is_copying and not self.is_canceling and copy_deadline and now_datetime() > copy_deadline:
				self.fail("copy_timeout", f"The copy did not reach cutover by {copy_deadline}.")
				self.request_destination_abort()
			time.sleep(self.poll_interval(status))

	def select_destination_metal_server(self) -> bool:
		"""Reserve the requested or an eligible destination. Return False to retry later."""
		if not self._rebalance_source_is_eligible():
			self.settle("aborted")
			return False
		virtual_machine: VirtualMachine = frappe.get_doc("Virtual Machine", self.migration.virtual_machine)
		shape = self.destination_shape(virtual_machine)
		has_sleep_checkpoint = self._prepare_sleep_migration(virtual_machine)
		requested_destination = self.migration.destination_metal_server
		# A destination that the operator names is not limited by the affinity rules.
		placement_rules = (
			AffinityRules()
			if requested_destination
			else AffinityRules.from_json(virtual_machine.placement_rules)
		)
		requirements = PlacementRequirements(
			shape.cpu_millicores,
			shape.memory_mib,
			shape.disk_mib,
			cast(str, virtual_machine.architecture),
			virtual_machine.tenant_id,
			shape.sleep_after_idle_seconds > 0,
			placement_rules=placement_rules,
			virtual_machine=virtual_machine.name,
		)
		exclude_servers = {self.migration.source_metal_server}

		try:
			if requested_destination:
				if requested_destination == self.migration.source_metal_server:
					raise AtlasUserError(
						_("Choose a destination Metal Server other than {0}.").format(requested_destination)
					)
				destination = PlacementStrategy.reserve_server(
					requirements, requested_destination, exclude_servers=exclude_servers
				)
			else:
				destination = PlacementStrategy.find_server(
					requirements,
					exclude_servers=exclude_servers,
					allowed_servers=self._load_allowed_destinations(),
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

		values: dict[str, Any] = {
			"destination_metal_server": destination,
			"status": "preparing",
			"started_at": now_datetime(),
			"destination_metal_server_selection_message": None,
			"progress_percent": 5,
		}
		if has_sleep_checkpoint:
			values["has_memory_snapshot"] = 1
			values["allow_disk_only_fallback"] = 0
		try:
			# A sleeping VM wants to run and keeps its saved memory; a stopped VM has none.
			if self.migration.has_memory_snapshot and virtual_machine.desired_state == "stopped":
				raise AtlasUserError(_("A stopped VM has no memory to move."))
			self.validate_compatibility(destination, virtual_machine.architecture, has_sleep_checkpoint)
		except AtlasUserError as error:
			if has_sleep_checkpoint or not self.migration.allow_disk_only_fallback:
				self.update(
					{
						"error_code": "migration_incompatible",
						"error_message": str(error),
						"error_at": now_datetime(),
					}
				)
				self.settle("failed")
				return False
			values["has_memory_snapshot"] = 0
			values["error_message"] = memory_snapshot_skipped_message(str(error))

		self.update(values)
		return self.migration.status not in TERMINAL_STATUSES and not self.is_canceling

	def _prepare_sleep_migration(self, virtual_machine: VirtualMachine) -> bool:
		if virtual_machine.sleep_after_idle_seconds <= 0:
			return False
		client = MetalClient(self.source_metal_server)
		information = client.get_virtual_machine(virtual_machine.name)
		if information.observed.sleep_state not in {"deep-sleeping", "coma"}:
			return False
		VirtualMachineSleepService(virtual_machine).refresh_urls(information, client)
		return True

	def validate_compatibility(
		self, destination: str, architecture: str, has_sleep_checkpoint: bool = False
	) -> None:
		"""Check synced host facts before a memory snapshot migration."""
		if not self.migration.has_memory_snapshot and not has_sleep_checkpoint:
			return
		if self.has_target_shape:
			raise AtlasUserError(_("A memory snapshot migration cannot resize the VM."))

		source = self._load_compatibility_report(self.migration.source_metal_server)
		target = self._load_compatibility_report(destination)
		self._validate_cpu_compatibility(source, target, architecture)
		self._validate_clock_compatibility(source, target)

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
		if self.migration.status in TERMINAL_STATUSES:
			return True
		state = status.get("status")
		if state == "completed":
			self.settle("completed")
			return True
		if state == "aborted":
			self.settle("failed" if self.has_failed else "aborted")
			return True
		if state == "failed":
			error = status.get("error") or {}
			self.fail(
				str(error.get("code") or "migration_error"),
				str(error.get("message") or "Metal failed migration."),
			)
			self.request_destination_abort()
			return False
		if state == "missing":
			return self.handle_missing_destination()
		if self.is_canceling:
			self.request_destination_abort()
			return False
		if state == "ready":
			if self.commit_destination():
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
			virtual_machine: VirtualMachine = frappe.get_doc(
				"Virtual Machine", self.migration.virtual_machine
			)
			resize = self.destination_shape(virtual_machine).migration_resize
		self.destination_client.put_migration(
			self.migration.name,
			self.migration.virtual_machine,
			source_coordination_url,
			resize,
			has_memory_snapshot=bool(self.migration.has_memory_snapshot),
			allow_disk_only_fallback=bool(self.migration.allow_disk_only_fallback),
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
		self.last_contact_at = now_datetime()
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
		if self.migration.has_memory_snapshot and response.get("memory_snapshot_status"):
			values["memory_snapshot_status"] = response["memory_snapshot_status"]
		# A definite failure keeps its own message.
		if response.get("memory_snapshot_error") and self.migration.error_code in (
			None,
			"",
			"metal_client_error",
		):
			values["error_message"] = memory_snapshot_skipped_message(response["memory_snapshot_error"])
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

	def commit_destination(self, *, is_rollback_refused: bool = False) -> bool:
		"""Commit the VM server, a resized shape, and finalizing status in one transaction.

		A canceling migration commits only when the destination refused rollback.
		"""
		virtual_machine, migration = self._lock_records()
		is_canceling = migration.status == "canceling" and (
			not is_rollback_refused or migration.error_code == DESTINATION_LOST_CODE
		)
		if is_canceling or migration.status in TERMINAL_STATUSES:
			self.migration = migration
			frappe.db.commit()  # nosemgrep
			return False
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

		# Public IP moves retry without undoing the committed destination.
		for allocation in frappe.get_all(
			"Public IP Allocation",
			filters={"virtual_machine": virtual_machine.name, "status": "Attached"},
			pluck="name",
		):
			try:
				frappe.get_doc("Public IP Allocation", allocation).move_to_server(
					migration.destination_metal_server
				)
			except Exception:
				frappe.log_error(
					title=f"Move public allocation {allocation} to {migration.destination_metal_server}"
				)
		return True

	def finish(self) -> None:
		"""Tell the destination that Atlas committed the VM."""
		self.destination_client.finish_migration(self.migration.name)

	def request_abort(self) -> None:
		"""Cancel a scheduled migration or request cleanup from its destination."""
		virtual_machine, self.migration = self._lock_records()
		if self.migration.status in TERMINAL_STATUSES:
			frappe.throw(_("This migration is already finished."), exc=AtlasUserError)
		if virtual_machine.server == self.migration.destination_metal_server:
			frappe.throw(
				_("This migration has committed its destination. It must finish."), exc=AtlasUserError
			)
		if self.migration.status == "scheduled":
			self.settle("aborted")
			return

		if not self.is_canceling:
			self.update({"status": "canceling"})
		frappe.db.commit()  # nosemgrep
		if self.migration.destination_metal_server:
			self.request_destination_abort()

	def request_destination_abort(self) -> None:
		"""Roll back before ownership changes; finish after the commit."""
		try:
			if (
				frappe.db.get_value("Virtual Machine", self.migration.virtual_machine, "server")
				== self.migration.destination_metal_server
			):
				self.finish()
			else:
				self.abort_or_take_destination()
		except MetalClientError as error:
			self.record_error(error)

	def abort_or_take_destination(self) -> None:
		"""Ask for rollback. A destination that already ran the guest refuses it, so the VM moves there."""
		if self.migration.error_code == DESTINATION_LOST_CODE:
			self.destination_client.discard_migration(self.migration.name)
			return
		try:
			self.destination_client.abort_migration(self.migration.name)
		except MetalClientError as error:
			if error.status != 409:
				raise
			if self.commit_destination(is_rollback_refused=True):
				self.finish()

	def restore_source(self) -> None:
		"""Start the stopped source again after an operator confirmed that the destination host is lost.

		The destination's writes since cutover are lost. A destination that returns later is discarded.
		"""
		virtual_machine, self.migration = self._lock_records()
		if self.migration.status in TERMINAL_STATUSES:
			frappe.throw(_("This migration is already finished."), exc=AtlasUserError)
		if (
			not self.migration.destination_metal_server
			or virtual_machine.server == self.migration.destination_metal_server
		):
			frappe.throw(
				_("This migration has no destination or has committed it. It cannot restore its source."),
				exc=AtlasUserError,
			)
		if self.is_destination_reporting():
			frappe.throw(
				_(
					"Metal Server {0} reported to Atlas in the last {1} minutes. Abort the migration instead."
				).format(
					self.migration.destination_metal_server, int(DESTINATION_LOST_AFTER.total_seconds() // 60)
				),
				exc=AtlasUserError,
			)
		self.update(
			{
				"status": "canceling",
				"error_code": DESTINATION_LOST_CODE,
				"error_message": "The destination host was lost. An operator restored the source.",
				"error_at": now_datetime(),
			},
			commit=True,
		)

		try:
			MetalClient(self.source_metal_server).restore_migration_source(
				self.migration.name, self.migration.virtual_machine
			)
		except MetalClientError as error:
			frappe.throw(_("The source host did not restore the VM: {0}. Retry.").format(str(error)))
		self.settle("failed")

	def is_destination_reporting(self) -> bool:
		"""Return whether the destination host sent a capacity sample recently."""
		return bool(
			frappe.db.exists(
				"Metal Server Usage",
				{
					"server": self.migration.destination_metal_server,
					"creation": (">=", now_datetime() - DESTINATION_LOST_AFTER),
				},
			)
		)

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

	def release_virtual_machine(self) -> None:
		"""Clear the VM action lock when this migration still holds it."""
		frappe.db.set_value(
			"Virtual Machine",
			{"name": self.migration.virtual_machine, "active_migration": self.migration.name},
			"active_migration",
			None,
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
		# A migration that completed after a disk-only fallback keeps its failed memory snapshot status.
		is_fallback = status == "completed" and self.migration.memory_snapshot_status == "failed"
		if self.migration.has_memory_snapshot and not is_fallback:
			values["memory_snapshot_status"] = status
		self.update(values)
		self.release_virtual_machine()
		frappe.db.commit()  # nosemgrep

		count("atlas_migrations_total", result=status)
		observe("atlas_migration_duration_seconds", values["duration_seconds"], result=status)

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
		virtual_machine, migration = self._lock_records()
		if (
			migration.status in TERMINAL_STATUSES
			or (
				values.get("error_code") == "metal_client_error"
				and migration.error_code not in (None, "", "metal_client_error")
			)
			or (migration.status == "canceling" and values.get("error_code") == "destination_unreachable")
		):
			self.migration = migration
			if commit:
				frappe.db.commit()  # nosemgrep
			return
		# Polling cannot undo cancellation or roll back committed ownership.
		if (
			values.get("status") == "canceling"
			and virtual_machine.server == migration.destination_metal_server
		):
			values = {**values, "status": "finalizing"}
		elif migration.status == "canceling" and values.get("status") not in TERMINAL_STATUSES:
			values = {**values, "status": "canceling"}
		for fieldname, value in values.items():
			setattr(migration, fieldname, value)
		if transfers is not None:
			self.upsert_transfers(migration, transfers)
		migration.flags.updated_by_vm_migration_worker = True
		migration.save(ignore_permissions=True)
		if commit:
			frappe.db.commit()  # nosemgrep
		self.migration = migration

	def _lock_records(self) -> tuple[VirtualMachine, VirtualMachineMigration]:
		"""Lock the VM before its migration for every controller write."""
		virtual_machine: VirtualMachine = frappe.get_doc(
			"Virtual Machine", self.migration.virtual_machine, for_update=True
		)
		migration: VirtualMachineMigration = frappe.get_doc(
			"Virtual Machine Migration", self.migration.name, for_update=True
		)
		return virtual_machine, migration

	@staticmethod
	def upsert_transfers(migration: VirtualMachineMigration, transfers: list[dict[str, Any]]) -> None:
		"""Copy Metal transfer values into their matching child rows."""
		rows = {int(row.idx): row for row in migration.transfers}
		for index, transfer in enumerate(transfers, start=1):
			sequence = int(transfer.get("sequence") or index)
			row = rows.get(sequence)
			if row is None:
				row = migration.append("transfers", {"idx": sequence})
			row.snapshot_type = "Memory" if transfer.get("snapshot_type") == "memory" else "Disk"
			row.started_at = timestamp_field(transfer, "started_at")
			row.finished_at = timestamp_field(transfer, "finished_at")
			row.duration_seconds = transfer.get("duration_seconds", 0)
			row.transferred_mib = transfer.get("transferred_mib", 0)
			row.total_mib = transfer.get("total_mib", 0)
			row.completed = transfer.get("completed", False)
			# Metal sends its source throttle limit, so Atlas measures throughput itself.
			elapsed_seconds = row.duration_seconds or (
				(now_datetime() - row.started_at).total_seconds() if row.started_at else 0
			)
			row.throughput_mibps = int(row.transferred_mib / elapsed_seconds) if elapsed_seconds > 0 else 0

	@property
	def has_target_shape(self) -> bool:
		"""Report whether the destination gives the VM a new shape."""
		return bool(self.migration.target_memory_mib)

	@property
	def is_canceling(self) -> bool:
		"""Return whether an operator or error requested cleanup."""
		return self.migration.status == "canceling"

	@property
	def has_failed(self) -> bool:
		"""Return whether an error, not an operator, ended this migration."""
		return self.migration.error_code not in (None, "", "metal_client_error")

	@property
	def is_expired(self) -> bool:
		"""Report whether the destination stayed silent for the visibility timeout."""
		last_contact_at = self.last_contact_at or self.migration.started_at
		return bool(
			last_contact_at
			and now_datetime() > get_datetime(last_contact_at) + DESTINATION_VISIBILITY_TIMEOUT
		)

	def copy_deadline(self) -> datetime | None:
		"""Return when a copy that has not reached cutover counts as stuck."""
		if not self.migration.started_at:
			return None
		disk_mib = frappe.db.get_value("Virtual Machine", self.migration.virtual_machine, "disk_mib")
		copy_duration = timedelta(seconds=disk_mib / MINIMUM_COPY_THROUGHPUT_MIBPS)
		return get_datetime(self.migration.started_at) + copy_duration + COPY_TIMEOUT_MARGIN

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
		server: MetalServer = frappe.get_doc("Metal Server", self.migration.destination_metal_server)
		return MetalClient(server)

	@property
	def source_metal_server(self) -> MetalServer:
		"""Return the source Metal Server record."""
		return frappe.get_doc("Metal Server", self.migration.source_metal_server)

	def _rebalance_source_is_eligible(self) -> bool:
		"""Release an automatic move if its source policy changed while it was queued."""
		if not self.migration.metal_server_drain or not frappe.db.get_value(
			"Metal Server Drain", self.migration.metal_server_drain, "is_sleepy_rebalance"
		):
			return True
		from atlas.vm.core.sleepy_rebalance import SleepyHostRebalance

		policy = SleepyHostRebalance(self.migration.source_metal_server)
		return policy.is_eligible(
			MetalClient(policy.server).get_virtual_machine(self.migration.virtual_machine)
		)

	def _load_allowed_destinations(self) -> set[str] | None:
		"""Return the destinations of the drain that owns this migration, or None for any."""
		if not self.migration.metal_server_drain:
			return None
		drain: MetalServerDrain = frappe.get_doc("Metal Server Drain", self.migration.metal_server_drain)
		if drain.is_sleepy_rebalance:
			from atlas.vm.core.sleepy_rebalance import SleepyHostRebalance

			machine = frappe.get_doc("Virtual Machine", self.migration.virtual_machine)
			policy = SleepyHostRebalance(
				self.migration.source_metal_server, allowed_destinations=drain.allowed_destinations
			)
			return {host.name for host in policy.destinations(machine)}
		return drain.allowed_destinations

	@staticmethod
	def _load_compatibility_report(server: str) -> MetalHostCompatibility:
		"""Load and validate one synced host report."""
		try:
			return MetalHostCompatibility.from_dict(
				frappe.parse_json(frappe.db.get_value("Metal Server", server, "host_compatibility_report"))
			)
		except ValueError as error:
			raise AtlasUserError(
				_("Metal Server {0} has no CPU compatibility report. Wait for host sync.").format(server)
			) from error

	@staticmethod
	def _validate_cpu_compatibility(
		source: MetalHostCompatibility, target: MetalHostCompatibility, architecture: str
	) -> None:
		"""Check the VM architecture and host CPU contract."""
		if source.architecture != architecture:
			raise AtlasUserError(_("The source CPU architecture does not match the VM."))

		fields = ("architecture", "cpu_profile", "cpu_vendor", "snapshot_host")
		if source.cpu_profile == "native":
			fields += ("cpu_model", "cpu_features")
		for field in fields:
			if getattr(source, field) != getattr(target, field):
				raise AtlasUserError(_("Migration hosts have different {0}.").format(field.replace("_", " ")))

	@staticmethod
	def _validate_clock_compatibility(source: MetalHostCompatibility, target: MetalHostCompatibility) -> None:
		"""Check stable x86 clocks and destination scaling support."""
		if source.architecture != "amd64":
			return
		if not source.tsc_khz or not target.tsc_khz:
			raise AtlasUserError(_("The migration hosts did not report a stable CPU clock."))

		# Firecracker accepts boot-time clock calibration differences up to 250 ppm.
		if (
			abs(source.tsc_khz - target.tsc_khz) > source.tsc_khz * 250 // 1_000_000
			and not target.tsc_scaling
		):
			raise AtlasUserError(_("The destination cannot scale the source CPU clock."))


def memory_snapshot_skipped_message(reason: str) -> str:
	"""Explain why a memory snapshot migration continued with the disk only."""
	return _("Memory snapshot skipped, migrating the disk only: {0}").format(reason)


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
	migration: VirtualMachineMigration = frappe.get_doc("Virtual Machine Migration", migration_name)
	service = MigrationService(migration)
	try:
		service.run()
	except Exception as error:
		frappe.db.rollback()
		frappe.log_error(
			title=f"Virtual Machine migration {migration_name} failed", message=frappe.get_traceback()
		)
		if is_transient_error(error):
			return
		service.fail("migration_worker_error", str(error))


def is_transient_error(error: Exception) -> bool:
	"""Report an error that the next reconcile can retry without failing the migration."""
	if isinstance(error, MetalClientError):
		return error.retryable
	return isinstance(error, frappe.QueryDeadlockError | frappe.QueryTimeoutError | JobTimeoutException)


@run_as_admin
def reconcile_migrations() -> None:
	"""Requeue every migration that still owns a VM action lock."""
	for name in get_active_migrations():
		reconcile_migration(name)
	discard_returned_destinations()


def discard_returned_destinations() -> None:
	"""Discard the destination of a migration whose source an operator restored, once its host reports again."""
	migrations = frappe.get_all(
		"Virtual Machine Migration",
		filters={
			"status": "failed",
			"error_code": DESTINATION_LOST_CODE,
			"modified": (">=", now_datetime() - LOST_DESTINATION_DISCARD_WINDOW),
		},
		pluck="name",
	)
	for name in migrations:
		service = MigrationService(frappe.get_doc("Virtual Machine Migration", name))
		if not service.is_destination_reporting():
			continue
		try:
			if service.destination_client.get_migration(name).get("status") != "aborted":
				service.destination_client.discard_migration(name)
		except MetalClientError as error:
			if not error.is_not_found:
				frappe.log_error(title=f"Discard of lost migration destination {name} failed")


def get_active_migrations(server: str | None = None) -> list[str]:
	"""Return the migrations that hold a VM action lock, optionally only for VMs on one server."""
	# Active Migration is a uuid column. The "is set" filter compares it with '', which matches no row.
	virtual_machine = frappe.qb.DocType("Virtual Machine")
	query = (
		frappe.qb.from_(virtual_machine)
		.select(virtual_machine.active_migration)
		.where(virtual_machine.active_migration.isnotnull())
	)
	if server:
		query = query.where(virtual_machine.server == server)
	return query.run(pluck=True)


def has_running_migration() -> bool:
	"""Return whether any migration has not finished."""
	return bool(
		frappe.db.exists("Virtual Machine Migration", {"status": ["not in", list(TERMINAL_STATUSES)]})
	)


def is_vm_migration_paused_for_create() -> bool:
	"""Read the pause flag under a shared row lock that lasts until commit.

	Pausing updates this row, so it waits for every create that passed this check.
	"""
	rows = frappe.db.sql(
		"""SELECT value FROM `tabSingles`
		WHERE doctype = 'Atlas Settings' AND field = 'is_vm_migration_paused'
		LOCK IN SHARE MODE"""
	)
	return bool(rows and int(rows[0][0] or 0))
