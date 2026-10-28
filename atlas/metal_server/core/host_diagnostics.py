from __future__ import annotations

import re
import shlex

import frappe
from frappe import _

from atlas.atlas.core.scrub import scrub
from atlas.atlas.core.ssh import SSHRunner

MACHINES_PATH = "/var/lib/metal/machines"
VIRTUAL_MACHINE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAXIMUM_LOG_BYTES = 256 * 1024
MAXIMUM_JOURNAL_LINES = 2_000
MAXIMUM_SINCE_MINUTES = 7 * 24 * 60
TIMEOUT_SECONDS = 60
HEALTH_SCRIPT = """\
section() { printf '\\n== %s ==\\n' "$1"; }
section uptime; uptime
section memory; free -m
section disks; df -h -x tmpfs -x devtmpfs -x overlay
section pools; zpool list 2>/dev/null
section units; systemctl is-active metal.service vector.service; systemctl --failed --no-pager --plain
section kernel; dmesg -T --level=emerg,alert,crit,err,warn | tail -n 50
section wireguard; wg show all dump 2>/dev/null | awk -v now="$(date +%s)" \\
	'NF == 9 { print $1, "peer", $5, "via", $4, "handshake", ($6 ? now - $6 "s ago" : "never") }'
section sockets; ss -s
"""


class HostDiagnostics:
	"""Fixed read-only scripts for the logs and live state of one host.

	A read returns its scrubbed output directly and stores no record.
	"""

	def __init__(self, server: str) -> None:
		self.server = server

	@classmethod
	def for_virtual_machine(cls, virtual_machine: str) -> HostDiagnostics:
		"""Return the diagnostics of the host that runs virtual_machine."""
		server = frappe.db.get_value("Virtual Machine", virtual_machine, "server")
		if not server:
			frappe.throw(_("Virtual Machine {0} has no host.").format(virtual_machine))
		return cls(server)

	def fetch_firecracker_log(self, virtual_machine: str, byte_count: int = 64 * 1024) -> dict:
		"""Read the end of the Firecracker log of one VM. Datum does not collect this file."""
		name = _validate_virtual_machine_name(virtual_machine)
		path = f"{MACHINES_PATH}/{name}/firecracker/{name}/root/firecracker.log"
		return self.read(f"tail -c {_bounded(byte_count, MAXIMUM_LOG_BYTES)} {shlex.quote(path)}")

	def fetch_virtual_machine_journal(
		self, virtual_machine: str, since_minutes: int = 60, line_count: int = 500
	) -> dict:
		"""Read the journal of the metal-vm unit of one VM, at all levels."""
		unit = f"metal-vm@{_validate_virtual_machine_name(virtual_machine)}.service"
		return self.read(_journal_script(unit, since_minutes, line_count))

	def fetch_metald_journal(
		self, since_minutes: int = 60, line_count: int = 500, pattern: str | None = None
	) -> dict:
		"""Read the metald journal. Datum has it too, unless the host telemetry is stale."""
		script = _journal_script("metal.service", since_minutes, line_count)
		if pattern:
			script += f" --grep {shlex.quote(pattern)}"
		return self.read(script)

	def check_health(self) -> dict:
		"""Read load, memory, disks, failed units, kernel errors, WireGuard, and sockets."""
		return self.read(HEALTH_SCRIPT)

	def read(self, script: str) -> dict:
		"""Run the script on the host over SSH and return its scrubbed output."""
		runner = SSHRunner(frappe.get_doc("Metal Server", self.server).ssh_host)
		result = runner.run_command(script, timeout_seconds=TIMEOUT_SECONDS)
		return {"host": self.server, "exit_code": result.exit_code, "output": scrub(result.output)}


def _journal_script(unit: str, since_minutes: int, line_count: int) -> str:
	since = f"-{_bounded(since_minutes, MAXIMUM_SINCE_MINUTES)}min"
	lines = _bounded(line_count, MAXIMUM_JOURNAL_LINES)
	return (
		f"journalctl --no-pager --output=short-iso --unit {shlex.quote(unit)} --since {since} --lines {lines}"
	)


def _bounded(value: int, maximum: int) -> int:
	if not 1 <= value <= maximum:
		frappe.throw(_("The value must be between 1 and {0}.").format(maximum))
	return value


def _validate_virtual_machine_name(name: str) -> str:
	if not VIRTUAL_MACHINE_NAME.match(name or ""):
		frappe.throw(_("{0} is not a valid Virtual Machine name.").format(name))
	return name
