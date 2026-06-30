"""Ad-hoc command fan-out over the existing SSH transport.

Atlas drives every host and guest through *verb* Tasks chosen from a fixed
catalog (`provision-vm`, `sync-image`, …). This module is the one place that
runs an **arbitrary** operator-typed command over the same transport — the
engine behind the SSH Console doctype and the per-form "Run Command" action.

It is deliberately controller-agnostic and stdlib-plus-`atlas.atlas.ssh` only
(no Frappe document logic), so its classification and fan-out unit-test in
milliseconds with no host (the spec's "host facts vs unit-covered logic" split).

The one behavioural departure from `run_task`: a failed command is a *result*,
never an exception. `run_task` raises so a controller can flip a doc's status;
the console's job is to report every target's outcome, so a non-zero exit is a
`Failure` row and a transport error is an `Unreachable` row — neither raises.
"""

from __future__ import annotations

import dataclasses
import subprocess
import time

import frappe

from atlas.atlas.ssh import (
	Connection,
	connection_for_guest,
	connection_for_server,
	run_ssh,
	ssh_key_file,
)

# The two SSH target kinds, matching the two Connection builders. Servers are
# reached over their public IPv4 as root; guests (Virtual Machines) over their
# public IPv6 /128 as root with the same key.
SERVER = "Server"
VIRTUAL_MACHINE = "Virtual Machine"
TARGET_KINDS = (SERVER, VIRTUAL_MACHINE)

# Result statuses, lifted from press's Ansible Console: a clean exit, a non-zero
# exit, and a host we never reached (SSH/transport error before an exit code).
SUCCESS = "Success"
FAILURE = "Failure"
UNREACHABLE = "Unreachable"

DEFAULT_TIMEOUT_SECONDS = 60


@dataclasses.dataclass(frozen=True)
class Target:
	"""One SSH destination: a Server or a Virtual Machine, by docname."""

	kind: str
	name: str

	def __post_init__(self) -> None:
		if self.kind not in TARGET_KINDS:
			frappe.throw(f"Unknown SSH target kind: {self.kind!r}")


@dataclasses.dataclass(frozen=True)
class CommandResult:
	"""The outcome of one command against one target. Maps 1:1 onto an
	`SSH Command Result` child row."""

	target_kind: str
	target_name: str
	status: str
	stdout: str
	stderr: str
	exit_code: int | None
	duration_milliseconds: int


def connection_for_target(target: Target) -> Connection:
	"""Build the SSH Connection for a target by loading its doc and dispatching
	to the matching transport builder — Server over v4, guest over v6 /128."""
	doc = frappe.get_doc(target.kind, target.name)
	if target.kind == SERVER:
		return connection_for_server(doc)
	return connection_for_guest(doc)


def run_on_target(
	target: Target,
	command: str,
	*,
	timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> CommandResult:
	"""Run one command on one target and return its outcome. Never raises for a
	remote failure: a non-zero exit is `Failure`, and any error reaching the host
	(missing address, connect timeout, transport error) is `Unreachable`."""
	start = time.monotonic()
	try:
		connection = connection_for_target(target)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			stdout, stderr, exit_code = run_ssh(
				connection, key_path, command, timeout_seconds=timeout_seconds
			)
	except subprocess.TimeoutExpired:
		return _unreachable(target, f"Command timed out after {timeout_seconds}s", start)
	except Exception as exception:
		return _unreachable(target, str(exception), start)

	return CommandResult(
		target_kind=target.kind,
		target_name=target.name,
		status=SUCCESS if exit_code == 0 else FAILURE,
		stdout=stdout,
		stderr=stderr,
		exit_code=exit_code,
		duration_milliseconds=_elapsed_ms(start),
	)


def run_fan_out(
	targets: list[Target],
	command: str,
	*,
	on_result,
	timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[CommandResult]:
	"""Run `command` on each target in order, calling `on_result(result)` after
	each so the caller can stream and persist incrementally. Sequential: SSH
	ControlMaster keeps back-to-back connects cheap and the operator watches rows
	fill in one by one. Returns every result.

	`on_result` is best-effort — a sink that raises must not abort the remaining
	targets (the same contract the streaming Task log sink follows)."""
	results: list[CommandResult] = []
	for target in targets:
		result = run_on_target(target, command, timeout_seconds=timeout_seconds)
		results.append(result)
		try:
			on_result(result)
		except Exception:
			frappe.logger("atlas").warning(
				f"ssh-console result sink raised for {target.kind} {target.name}; continuing"
			)
	return results


def _unreachable(target: Target, message: str, start: float) -> CommandResult:
	return CommandResult(
		target_kind=target.kind,
		target_name=target.name,
		status=UNREACHABLE,
		stdout="",
		stderr=message,
		exit_code=None,
		duration_milliseconds=_elapsed_ms(start),
	)


def _elapsed_ms(start: float) -> int:
	return int((time.monotonic() - start) * 1000)
