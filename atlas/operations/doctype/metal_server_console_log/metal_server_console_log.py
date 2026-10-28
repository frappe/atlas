# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.scrub import scrub, scrub_complete_lines
from atlas.atlas.core.ssh import SSHRunner

if TYPE_CHECKING:
	from atlas.operations.doctype.metal_server_console_result.metal_server_console_result import (
		MetalServerConsoleResult,
	)

SAVE_INTERVAL_SECONDS = 1
JOB_TIMEOUT_BUFFER_SECONDS = 60
SERVER_LOOKUP_FIELDS = (
	"name",
	"title",
	"public_ipv4_address",
	"private_ipv4_address",
	"wireguard_ip_address",
)


class MetalServerConsoleLog(Document):
	"""One script run as root on many Metal Servers at once, with the result of each server."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.operations.doctype.metal_server_console_result.metal_server_console_result import (
			MetalServerConsoleResult,
		)

		ended_at: DF.Datetime | None
		results: DF.Table[MetalServerConsoleResult]
		script: DF.Code
		scrub_output: DF.Check
		servers: DF.SmallText
		started_at: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Success", "Failed"]
		timeout_seconds: DF.Int
	# end: auto-generated types

	def before_insert(self) -> None:
		for server in self._find_servers():
			ssh_host = frappe.get_doc("Metal Server", server).ssh_host
			self.append("results", {"server": server, "ssh_host": ssh_host, "status": "Pending"})

	def validate(self) -> None:
		if not 1 <= self.timeout_seconds <= 3_600:
			frappe.throw(_("Timeout must be between 1 and 3600 seconds."))

	def after_insert(self) -> None:
		if self.flags.run_in_background is False:
			self.execute()
			return

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"execute",
			queue="long",
			timeout=self.timeout_seconds + JOB_TIMEOUT_BUFFER_SECONDS,
			enqueue_after_commit=True,
		)

	@run_as_admin
	def execute(self) -> None:
		"""Run the script on all servers at once. Save and publish the output each second."""
		self.db_set({"status": "Running", "started_at": now_datetime()}, commit=True)
		self._publish_progress([])
		server_runs = [ServerRun(row, bool(self.scrub_output)) for row in self.results]
		with ThreadPoolExecutor(max_workers=len(server_runs)) as executor:
			futures = [executor.submit(run.execute, self.script, self.timeout_seconds) for run in server_runs]
			pending = futures
			while pending:
				pending = wait(pending, timeout=SAVE_INTERVAL_SECONDS).not_done
				self._save_progress(server_runs)

		is_success = all(run.status == "Success" for run in server_runs)
		self.db_set(
			{"status": "Success" if is_success else "Failed", "ended_at": now_datetime()}, commit=True
		)
		self._publish_progress([])
		for future in futures:
			future.result()

	def _find_servers(self) -> list[str]:
		"""Return the Metal Server for each title, name, or IP address in the servers field."""
		entries = dict.fromkeys(entry for entry in re.split(r"[,\s]+", self.servers or "") if entry)
		if not entries:
			frappe.throw(_("Add at least one server."))

		servers = []
		for entry in entries:
			matches = frappe.get_all(
				"Metal Server",
				filters={"status": ("!=", "Deleted")},
				or_filters={field: entry for field in SERVER_LOOKUP_FIELDS},
				pluck="name",
			)
			if len(matches) != 1:
				frappe.throw(
					_("{0} matches {1} Metal Servers. It must match one.").format(entry, len(matches))
				)
			servers.append(matches[0])
		return list(dict.fromkeys(servers))

	def _save_progress(self, server_runs: list[ServerRun]) -> None:
		changes = [change for run in server_runs if (change := run.save())]
		if changes:
			frappe.db.commit()  # nosemgrep
			self._publish_progress(changes)

	def _publish_progress(self, changes: list[dict]) -> None:
		"""Send changed values to the open form. A form reload would close an open row."""
		frappe.publish_realtime(
			"metal_server_console_log_update",
			{
				"name": self.name,
				"log": {field: self.get(field) for field in ("status", "started_at", "ended_at")},
				"results": changes,
			},
			doctype=self.doctype,
			docname=self.name,
		)


class ServerRun:
	"""The script run on one server. Its thread writes only to this object, and the job thread saves it."""

	def __init__(self, row: MetalServerConsoleResult, scrub_output: bool = False) -> None:
		self.row = row
		self.scrub_output = scrub_output
		self.output_chunks: list[str] = []
		self.exit_code: int | None = None
		self.is_started = False
		self.is_finished = False

	@property
	def status(self) -> str:
		"""Return the row status for this run."""
		if self.is_finished:
			return "Success" if self.exit_code == 0 else "Failed"
		return "Running" if self.is_started else "Pending"

	def execute(self, script: str, timeout_seconds: int) -> None:
		self.is_started = True
		try:
			result = SSHRunner(self.row.ssh_host).run_command(
				script, timeout_seconds=timeout_seconds, on_output=self.output_chunks.append
			)
			self.exit_code = result.exit_code
		except (OSError, subprocess.TimeoutExpired) as error:
			self.output_chunks.append(f"\n{error}")
		finally:
			self.is_finished = True

	def save(self) -> dict | None:
		"""Save new output and status to the row. Return the change for the form, or None."""
		# Read the flag first: output joined after the run ends is complete.
		is_finished = self.is_finished
		output = "".join(self.output_chunks)
		if self.scrub_output:
			# A running output keeps its last line back, so the saved text only grows.
			output = scrub(output) if is_finished else scrub_complete_lines(output)
		values = {"output": output, "status": self.status}
		# The exit code column cannot be null. A run without an exit code keeps the default.
		if self.exit_code is not None:
			values["exit_code"] = self.exit_code
		if all(self.row.get(field) == value for field, value in values.items()):
			return None

		offset = len(self.row.output or "")
		self.row.db_set(values, update_modified=False)
		return {
			"name": self.row.name,
			"status": self.row.status,
			"exit_code": self.row.exit_code,
			"offset": offset,
			"new_output": output[offset:],
		}
