from __future__ import annotations

import os
import selectors
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep


@dataclass(frozen=True, slots=True)
class SSHResult:
	"""The output from one SSH execution."""

	output: str
	exit_code: int | None

	@property
	def is_success(self) -> bool:
		"""Return true when the remote command completed successfully."""
		return self.exit_code == 0


class SSHRunner:
	"""Run shell scripts on one SSH host."""

	def __init__(self, host: str, port: int = 22, user: str = "root") -> None:
		if not host:
			raise ValueError("SSH host is required")
		if not 1 <= port <= 65_535:
			raise ValueError(f"Invalid SSH port: {port}")
		if not user:
			raise ValueError("SSH user is required")

		self.host = host
		self.port = port
		self.user = user

	def run_command(
		self,
		command: str,
		*,
		data: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
		on_output: Callable[[str], None] | None = None,
	) -> SSHResult:
		"""Run a shell command and return its output."""
		return self._run(command, data or {}, timeout_seconds, on_output)

	def run_script(
		self,
		script: str,
		*,
		data: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
		on_output: Callable[[str], None] | None = None,
	) -> SSHResult:
		"""Run a script file and return its output."""
		return self._run(self.get_script_source(script), data or {}, timeout_seconds, on_output)

	def _run(
		self,
		source: str,
		data: Mapping[str, object],
		timeout_seconds: int,
		on_output: Callable[[str], None] | None,
	) -> SSHResult:
		if timeout_seconds <= 0:
			raise ValueError(f"Invalid SSH timeout: {timeout_seconds}")

		process = subprocess.Popen(
			[
				"ssh",
				"-p",
				str(self.port),
				"-o",
				"StrictHostKeyChecking=no",
				"-o",
				"UserKnownHostsFile=/dev/null",
				"-o",
				"GlobalKnownHostsFile=/dev/null",
				"-o",
				"LogLevel=ERROR",
				"-o",
				"BatchMode=yes",
				"-o",
				"ConnectTimeout=30",
				f"{self.user}@{self.host}",
				"bash -s",
			],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
		)
		assert process.stdin is not None
		process.stdin.write(self._script_with_data(source, data).encode())
		process.stdin.close()

		output = self._read_output(process, timeout_seconds, on_output)
		return SSHResult(output=output, exit_code=process.wait())

	@staticmethod
	def _read_output(
		process: subprocess.Popen[bytes], timeout_seconds: int, on_output: Callable[[str], None] | None
	) -> str:
		assert process.stdout is not None
		output: list[str] = []
		deadline = monotonic() + timeout_seconds
		with selectors.DefaultSelector() as selector:
			selector.register(process.stdout, selectors.EVENT_READ)
			while selector.get_map():
				remaining = deadline - monotonic()
				if remaining <= 0:
					process.kill()
					process.wait()
					raise subprocess.TimeoutExpired(process.args, timeout_seconds, output="".join(output))
				for key, _ in selector.select(remaining):
					chunk = os.read(key.fd, 4_096)
					if not chunk:
						selector.unregister(key.fileobj)
						continue
					text = chunk.decode(errors="replace")
					output.append(text)
					if on_output:
						on_output(text)
		return "".join(output)

	@staticmethod
	def _script_with_data(script: str, data: Mapping[str, object]) -> str:
		exports = []
		for name, value in data.items():
			if not name.isidentifier():
				raise ValueError(f"Invalid SSH data key: {name}")
			exports.append(f"export {name}={shlex.quote(str(value))}")
		return "\n".join((*exports, script))

	@staticmethod
	def get_script_source(script: str) -> str:
		"""Return the source of a script file in scripts/."""
		relative_path = Path(script)
		if relative_path.is_absolute() or ".." in relative_path.parts:
			raise ValueError(f"Invalid SSH script file: {script}")
		path = Path(__file__).resolve().parents[2] / "scripts" / relative_path
		if not path.is_file():
			raise ValueError(f"SSH script file does not exist: {script}")
		return path.read_text()


def wait_for_server(
	*, host: str, users: tuple[str, ...], timeout_seconds: int, poll_interval_seconds: int
) -> str:
	"""Return the first SSH user that becomes available on a server."""
	if not users:
		raise ValueError("At least one SSH user is required")
	if timeout_seconds <= 0:
		raise ValueError(f"Invalid SSH timeout: {timeout_seconds}")
	if poll_interval_seconds <= 0:
		raise ValueError(f"Invalid SSH poll interval: {poll_interval_seconds}")

	deadline = monotonic() + timeout_seconds
	while monotonic() < deadline:
		for user in users:
			if _ssh_is_available(host, user):
				return user
		sleep(poll_interval_seconds)

	raise TimeoutError(f"SSH did not become ready within {timeout_seconds} seconds")


def _ssh_is_available(host: str, user: str) -> bool:
	try:
		return SSHRunner(host, user=user).run_command("true", timeout_seconds=10).is_success
	except OSError, subprocess.TimeoutExpired:
		return False
