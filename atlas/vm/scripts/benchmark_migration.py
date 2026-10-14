#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import ipaddress
import json
import math
import os
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


@dataclasses.dataclass(frozen=True)
class Sample:
	"""Record one SSH probe using the observer's clock."""

	started: float
	finished: float
	started_at: str
	finished_at: str
	healthy: bool


class Outages:
	"""Collect gaps between successful SSH responses."""

	def __init__(self) -> None:
		self.last_success: Sample | None = None
		self.first_failure: Sample | None = None
		self.windows: list[dict] = []

	def observe(self, sample: Sample) -> dict | None:
		if not sample.healthy:
			if self.last_success is not None and self.first_failure is None:
				self.first_failure = sample
			return None

		window = None
		if self.first_failure is not None and self.last_success is not None:
			window = {
				"last_success_at": self.last_success.finished_at,
				"first_failed_probe_at": self.first_failure.started_at,
				"first_failure_observed_at": self.first_failure.finished_at,
				"recovered_at": sample.finished_at,
				"ssh_gap_seconds": sample.finished - self.last_success.finished,
				"failed_probe_to_recovery_seconds": sample.finished - self.first_failure.started,
			}
			self.windows.append(window)
			self.first_failure = None
		self.last_success = sample
		return window


def emit(event: str, **values: object) -> None:
	"""Write one JSON event without buffering."""
	print(json.dumps({"event": event, **values}), flush=True)


def probe_command(arguments: argparse.Namespace) -> list[str]:
	"""Build a fresh SSH connection without a multiplexed session."""
	if arguments.identity_file:
		return [
			"ssh",
			"-n",
			"-T",
			"-o",
			"BatchMode=yes",
			"-o",
			"IdentitiesOnly=yes",
			"-o",
			"StrictHostKeyChecking=yes",
			"-o",
			"ControlMaster=no",
			"-o",
			"ControlPath=none",
			"-o",
			"ConnectionAttempts=1",
			"-o",
			"ConnectTimeout=1",
			"-i",
			arguments.identity_file,
			f"{arguments.user}@{arguments.guest_address}",
			"true",
		]
	return [
		"ssh-keyscan",
		"-T",
		str(math.ceil(arguments.probe_timeout)),
		"-t",
		"ed25519",
		arguments.guest_address,
	]


def monitor(arguments: argparse.Namespace) -> None:
	"""Measure SSH availability on the host until the controller sends stop."""
	finish = threading.Event()
	cancel = threading.Event()

	def read_control() -> None:
		for line in sys.stdin:
			if line.strip() == "stop":
				finish.set()
			elif line.strip() == "cancel":
				cancel.set()
		if not finish.is_set():
			cancel.set()

	threading.Thread(target=read_control, daemon=True).start()
	for number in (signal.SIGINT, signal.SIGTERM):
		signal.signal(number, lambda *_: cancel.set())

	command = probe_command(arguments)
	outages = Outages()
	latencies: list[float] = []
	started = time.monotonic()
	consecutive_successes = 0
	ready = False
	while not cancel.is_set() and time.monotonic() - started < arguments.timeout:
		probe_started = time.monotonic()
		started_at = datetime.datetime.now(datetime.UTC).isoformat()
		try:
			response = subprocess.run(command, capture_output=True, timeout=arguments.probe_timeout)
			healthy = response.returncode == 0 and (
				bool(response.stdout.strip()) or bool(arguments.identity_file)
			)
		except subprocess.TimeoutExpired:
			healthy = False
		sample = Sample(
			probe_started,
			time.monotonic(),
			started_at,
			datetime.datetime.now(datetime.UTC).isoformat(),
			healthy,
		)
		window = outages.observe(sample)
		if window is not None:
			emit("recovered", **window)
		consecutive_successes = consecutive_successes + 1 if healthy else 0
		if healthy and not ready:
			latencies.append(sample.finished - sample.started)
		if not ready and consecutive_successes >= 3:
			ready = True
			outages.windows.clear()
			emit("ready", baseline_probe_seconds=statistics.median(latencies))
		if finish.is_set() and ready and consecutive_successes >= 3:
			break
		if not ready and time.monotonic() - started >= 30:
			break
		time.sleep(max(0, arguments.interval - (time.monotonic() - probe_started)))

	emit(
		"summary",
		ready=ready,
		canceled=cancel.is_set(),
		healthy=consecutive_successes >= 3,
		unrecovered=outages.first_failure is not None,
		windows=outages.windows,
		ssh_gap_seconds=sum(window["ssh_gap_seconds"] for window in outages.windows),
		interval_seconds=arguments.interval,
		probe_timeout_seconds=arguments.probe_timeout,
		probe="authenticated_ssh" if arguments.identity_file else "ssh_key_exchange",
	)


@contextlib.contextmanager
def remote_monitor(arguments: argparse.Namespace) -> Iterator[subprocess.Popen[str]]:
	"""Copy the observer to a temporary directory and remove it on exit."""
	ssh = [
		"ssh",
		"-o",
		"BatchMode=yes",
		"-o",
		"ConnectTimeout=10",
		"-o",
		"ServerAliveInterval=5",
		"-o",
		"ServerAliveCountMax=3",
		arguments.observer,
	]
	directory = subprocess.check_output(
		[*ssh, "mktemp -d /tmp/atlas-migration-benchmark.XXXXXXXX"], text=True, timeout=20
	).strip()
	if not directory.startswith("/tmp/atlas-migration-benchmark.") or "/" in directory.removeprefix("/tmp/"):
		raise RuntimeError("Observer returned an unexpected temporary directory")
	path = directory + "/benchmark.py"
	process = None
	try:
		subprocess.run(
			[*ssh, "umask 077; cat > " + shlex.quote(path)],
			input=Path(__file__).read_text(),
			text=True,
			check=True,
			timeout=20,
		)
		command = [
			"python3",
			"-u",
			path,
			"probe",
			"--guest-address",
			arguments.guest_address,
			"--interval",
			str(arguments.interval),
			"--probe-timeout",
			str(arguments.probe_timeout),
			"--timeout",
			str(arguments.timeout + 60),
			"--user",
			arguments.user,
		]
		if arguments.identity_file:
			command += ["--identity-file", arguments.identity_file]
		process = subprocess.Popen(
			[*ssh, shlex.join(command)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
		)
		yield process
	finally:
		if process is not None and process.poll() is None:
			process.terminate()
			try:
				process.wait(timeout=10)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait()
		subprocess.run(
			[*ssh, "rm -f " + shlex.quote(path) + "; rmdir " + shlex.quote(directory)], check=True, timeout=20
		)


def run_comparison(arguments: argparse.Namespace) -> None:
	"""Run memory and disk-only migrations in both directions through Atlas."""
	import frappe

	if not Path("sites").is_dir():
		raise RuntimeError("Run this command from the Frappe bench directory")
	with Path(arguments.output).open("x") as output:
		os.chdir("sites")
		frappe.init(site=arguments.site)
		frappe.connect()
		try:
			virtual_machine = frappe.get_doc("Virtual Machine", arguments.virtual_machine)
			if (
				virtual_machine.server not in (arguments.host_a, arguments.host_b)
				or virtual_machine.active_migration
			):
				raise RuntimeError("The VM must be on one of the two hosts, without an active migration")
			if virtual_machine.current_state != "running":
				raise RuntimeError("The VM must be running")
			for repetition in range(1, arguments.rounds + 1):
				for memory in (True, False):
					for _ in range(2):
						virtual_machine = frappe.get_doc("Virtual Machine", arguments.virtual_machine)
						source = virtual_machine.server
						if (
							source not in (arguments.host_a, arguments.host_b)
							or virtual_machine.active_migration
						):
							raise RuntimeError("VM ownership changed or another migration started")
						destination = arguments.host_b if source == arguments.host_a else arguments.host_a
						result = measure_migration(arguments, virtual_machine, destination, memory)
						result.update(
							repetition=repetition,
							source=source,
							destination=destination,
							has_memory_snapshot=memory,
						)
						output.write(json.dumps(result, default=str) + "\n")
						output.flush()
						emit("result", **result)
						if (
							result["status"] != "completed"
							or not result["ssh"]["healthy"]
							or result["ssh"]["unrecovered"]
						):
							raise RuntimeError(
								"Comparison stopped; inspect the migration and recorded SSH results"
							)
		finally:
			frappe.destroy()


def measure_migration(
	arguments: argparse.Namespace, virtual_machine: VirtualMachine, destination: str, memory: bool
) -> dict:
	"""Keep the observer running through migration completion and SSH recovery."""
	import frappe

	with remote_monitor(arguments) as observer:
		for line in observer.stdout:
			event = json.loads(line)
			if event["event"] == "ready":
				break
		else:
			raise RuntimeError("Observer could not establish a healthy SSH baseline")
		started = time.monotonic()
		migration_id = virtual_machine.migrate(
			destination_metal_server=destination, has_memory_snapshot=memory
		)
		frappe.db.commit()  # nosemgrep
		emit("migration_started", migration_id=migration_id, memory=memory, observer=arguments.observer)
		while True:
			frappe.db.rollback()
			migration = frappe.get_doc("Virtual Machine Migration", migration_id)
			if migration.status in ("completed", "failed", "aborted"):
				break
			if observer.poll() is not None:
				raise RuntimeError(f"SSH observer exited during migration {migration_id}")
			if time.monotonic() - started > arguments.timeout:
				raise TimeoutError(
					f"Migration {migration_id} remains {migration.status}; it was not canceled"
				)
			time.sleep(1)
		controller_seconds = time.monotonic() - started
		remaining = max(5, arguments.timeout + 60 - controller_seconds)
		stdout, _ = observer.communicate(input="stop\n", timeout=remaining)
		if observer.returncode != 0:
			raise RuntimeError("SSH observer failed")
		events = [json.loads(line) for line in stdout.splitlines()]
		if not events or events[-1]["event"] != "summary":
			raise RuntimeError("SSH observer returned no summary")
		return {
			"migration_id": migration_id,
			"status": migration.status,
			"migration_duration_seconds": migration.duration_seconds,
			"controller_duration_seconds": controller_seconds,
			"ssh": events[-1],
		}


def self_test() -> None:
	"""Check that startup failures and separate outages do not become one gap."""
	outages = Outages()
	for sample in (
		Sample(0, 0.1, "0", "0.1", False),
		Sample(1, 1.1, "1", "1.1", True),
		Sample(2, 3, "2", "3", False),
		Sample(3.5, 4.5, "3.5", "4.5", False),
		Sample(5, 5.1, "5", "5.1", True),
		Sample(6, 6.1, "6", "6.1", True),
		Sample(7, 8, "7", "8", False),
		Sample(9, 9.1, "9", "9.1", True),
	):
		outages.observe(sample)
	assert len(outages.windows) == 2
	assert math.isclose(outages.windows[0]["ssh_gap_seconds"], 4)
	assert math.isclose(outages.windows[0]["failed_probe_to_recovery_seconds"], 3.1)
	assert math.isclose(outages.windows[1]["ssh_gap_seconds"], 3)
	assert outages.first_failure is None
	emit("self_test", passed=True)


def main() -> None:
	"""Parse the controller or host observer command."""
	parser = argparse.ArgumentParser(description="Compare migration duration and host-measured SSH downtime.")
	commands = parser.add_subparsers(dest="command", required=True)
	commands.add_parser("self-test")
	for name in ("run", "probe"):
		command = commands.add_parser(name)
		command.add_argument("--guest-address", required=True, type=ipaddress.ip_address)
		command.add_argument("--interval", type=float, default=0.25)
		command.add_argument("--probe-timeout", type=float, default=1)
		command.add_argument("--timeout", type=float, default=600)
		command.add_argument("--identity-file", help="Existing guest private key path on the observer host")
		command.add_argument("--user", default="root")
		if name == "run":
			command.add_argument("--site", required=True)
			command.add_argument("--virtual-machine", required=True)
			command.add_argument("--host-a", required=True)
			command.add_argument("--host-b", required=True)
			command.add_argument(
				"--observer", required=True, help="SSH destination for the fixed observer host"
			)
			command.add_argument("--rounds", type=int, default=1)
			command.add_argument(
				"--output", required=True, help="New JSONL file; existing files are not overwritten"
			)
	arguments = parser.parse_args()
	if arguments.command == "self-test":
		self_test()
		return
	arguments.guest_address = str(arguments.guest_address)
	if any(
		not math.isfinite(value) or value <= 0
		for value in (arguments.interval, arguments.probe_timeout, arguments.timeout)
	):
		parser.error("Intervals and timeouts must be finite and positive")
	if arguments.command == "run" and (arguments.rounds < 1 or arguments.host_a == arguments.host_b):
		parser.error("Use positive rounds and two different hosts")
	if arguments.command == "probe":
		monitor(arguments)
	else:
		run_comparison(arguments)


if __name__ == "__main__":
	main()
