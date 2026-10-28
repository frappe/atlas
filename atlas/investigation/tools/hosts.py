from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import frappe
from frappe import _
from frappe.flow import ToolContext

from atlas.atlas.core.scrub import scrub
from atlas.atlas.core.ssh import SSHRunner
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.investigation.core.command_policy import validate_read_only_command
from atlas.investigation.tools.decorators import read_tool
from atlas.investigation.tools.inventory import get_host_name
from atlas.metal_server.core.host_diagnostics import TIMEOUT_SECONDS, HostDiagnostics

if TYPE_CHECKING:
	from atlas.operations.doctype.metal_server_console_log.metal_server_console_log import (
		MetalServerConsoleLog,
	)


@read_tool(background=True)
def read_firecracker_log(
	virtual_machine: Annotated[str, "Virtual Machine name."],
	byte_count: Annotated[int, "Bytes from the end of the file, at most 262144."] = 32_768,
	*,
	ctx: ToolContext,
) -> dict:
	"""Read the end of the Firecracker log of a VM on its host. Datum does not have this file."""
	return HostDiagnostics.for_virtual_machine(virtual_machine).fetch_firecracker_log(
		virtual_machine, byte_count
	)


@read_tool(background=True)
def read_virtual_machine_journal(
	virtual_machine: Annotated[str, "Virtual Machine name."],
	since_minutes: Annotated[int, "How far back to read, in minutes."] = 60,
	line_count: Annotated[int, "Newest lines to return, at most 2000."] = 300,
	*,
	ctx: ToolContext,
) -> dict:
	"""Read the journal of the metal-vm unit of a VM at all levels. Datum has only warnings and errors."""
	return HostDiagnostics.for_virtual_machine(virtual_machine).fetch_virtual_machine_journal(
		virtual_machine, since_minutes, line_count
	)


@read_tool(background=True)
def read_metald_journal(
	host: Annotated[str, "Metal Server name or title."],
	since_minutes: Annotated[int, "How far back to read, in minutes."] = 60,
	line_count: Annotated[int, "Newest lines to return, at most 2000."] = 300,
	pattern: Annotated[str | None, "Only lines that match this regular expression."] = None,
	*,
	ctx: ToolContext,
) -> dict:
	"""Read the metald journal on a host. Use it when Datum logs of the host are stale."""
	return HostDiagnostics(get_host_name(host)).fetch_metald_journal(since_minutes, line_count, pattern)


@read_tool(background=True)
def check_host_health(host: Annotated[str, "Metal Server name or title."], *, ctx: ToolContext) -> dict:
	"""Read live load, memory, disks, ZFS pools, failed units, kernel errors, WireGuard handshakes, and sockets."""
	return HostDiagnostics(get_host_name(host)).check_health()


@read_tool(background=True)
def run_read_only_command(
	target: Annotated[str, "Metal Server name or title, or the name of a tenant 0 VM."],
	command: Annotated[str, "A read-only shell command, such as 'journalctl -u vector --since -1h | tail'."],
	*,
	ctx: ToolContext,
) -> dict:
	"""Run a read-only command as root on a host or a tenant 0 VM, and return its scrubbed output.

	Atlas refuses a command that is not on its read-only list: no redirection, no command substitution, and only programs such as cat, grep, journalctl, ss, ip, systemctl status, wg show, and zpool status.
	"""
	validate_read_only_command(command)
	if not _is_virtual_machine(target):
		return HostDiagnostics(get_host_name(target)).read(command)

	virtual_machine = frappe.get_doc("Virtual Machine", target)
	runner = SSHRunner(virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command())
	result = runner.run_command(command, timeout_seconds=TIMEOUT_SECONDS)
	return {"virtual_machine": target, "exit_code": result.exit_code, "output": scrub(result.output)}


def run_on_target(target: str, command: str) -> dict:
	"""Run a command that can change a host or a VM, and record it as a console log or an SSH Task."""
	if not _is_virtual_machine(target):
		return console_log_result(
			HostDiagnostics(get_host_name(target)).run(command, run_in_background=False)
		)

	task = SSHTask.create_for_command(
		target_type="Virtual Machine",
		target=target,
		command=command,
		timeout_seconds=TIMEOUT_SECONDS,
		run_in_background=False,
	)
	task.reload()
	return {"ssh_task": task.name, "status": task.status, "exit_code": task.exit_code, "output": task.output}


def _is_virtual_machine(target: str) -> bool:
	"""Return whether the target is a tenant 0 VM. Refuse a customer VM: Atlas has no SSH key on it."""
	tenant_id = frappe.db.get_value("Virtual Machine", target, "tenant_id")
	if tenant_id not in (None, 0):
		frappe.throw(
			_("{0} belongs to tenant {1}. Atlas cannot connect to a customer VM.").format(target, tenant_id)
		)
	return tenant_id == 0


def console_log_result(log: MetalServerConsoleLog) -> dict:
	log.reload()
	result = log.results[0]
	return {
		"console_log": log.name,
		"host": result.server,
		"status": result.status,
		"exit_code": result.exit_code,
		"output": result.output,
	}
