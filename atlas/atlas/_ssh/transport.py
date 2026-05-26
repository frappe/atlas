"""SSH/SCP subprocess plumbing.

This module hides all the system-`ssh`/`scp` invocations behind small helpers.
Higher layers (runner.py) compose these to drive Task lifecycles without
knowing anything about ssh option strings or tempfile lifetimes for keys.
"""

import dataclasses
import os
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import frappe

KNOWN_HOSTS_PATH = Path("~/.atlas/known_hosts").expanduser()
REMOTE_STAGING_DIRECTORY = "/tmp/atlas"

SSH_OPTIONS = [
	"-o", "StrictHostKeyChecking=accept-new",
	"-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
	"-o", "BatchMode=yes",
	"-o", "ConnectTimeout=30",
]


@dataclasses.dataclass(frozen=True)
class Connection:
	host: str
	ssh_private_key: str
	user: str = "root"


def wait_for_ssh(connection: Connection, timeout_seconds: int = 300, poll_seconds: int = 5) -> None:
	"""Poll the host until SSH accepts a `true` command, or raise."""
	_ensure_known_hosts_directory()
	deadline = time.monotonic() + timeout_seconds
	with ssh_key_file(connection.ssh_private_key) as key_path:
		while True:
			_, _, exit_code = run_ssh(connection, key_path, "true", timeout_seconds=30)
			if exit_code == 0:
				return
			if time.monotonic() >= deadline:
				raise frappe.ValidationError(
					f"SSH to {connection.host} not ready after {timeout_seconds}s"
				)
			time.sleep(poll_seconds)


def upload_files(connection: Connection, files: list[tuple[str, str]]) -> None:
	"""scp files to the server. `files` is (local_path, remote_path) pairs.

	Not recorded as a Task. The remote parent directory is created first via
	a single SSH call so callers don't have to think about mkdir order.
	"""
	if not files:
		return

	_ensure_known_hosts_directory()
	with ssh_key_file(connection.ssh_private_key) as key_path:
		remote_dirs = sorted({os.path.dirname(remote) for _, remote in files if os.path.dirname(remote)})
		if remote_dirs:
			mkdir_command = "mkdir -p " + " ".join(shlex.quote(d) for d in remote_dirs)
			run_ssh(connection, key_path, mkdir_command, timeout_seconds=60)

		for local, remote in files:
			run_scp(connection, key_path, local, remote, timeout_seconds=300)


def run_ssh(
	connection: Connection,
	key_path: str,
	remote_command: str,
	timeout_seconds: int,
) -> tuple[str, str, int]:
	args = [
		"ssh",
		"-i", key_path,
		*SSH_OPTIONS,
		f"{connection.user}@{connection.host}",
		remote_command,
	]
	result = subprocess.run(
		args,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
	)
	return result.stdout, result.stderr, result.returncode


def run_scp(
	connection: Connection,
	key_path: str,
	local_path: str,
	remote_path: str,
	timeout_seconds: int,
) -> None:
	args = [
		"scp",
		"-i", key_path,
		*SSH_OPTIONS,
		local_path,
		f"{connection.user}@{connection.host}:{remote_path}",
	]
	result = subprocess.run(
		args,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
	)
	if result.returncode != 0:
		raise frappe.ValidationError(
			f"scp {local_path} -> {remote_path} failed: {result.stderr}"
		)


@contextmanager
def ssh_key_file(private_key: str):
	"""Write the SSH private key to a 0600 tempfile; delete it on exit."""
	handle = tempfile.NamedTemporaryFile(
		mode="w", delete=False, prefix="atlas-ssh-", suffix=".key"
	)
	try:
		os.chmod(handle.name, 0o600)
		key = private_key if private_key.endswith("\n") else private_key + "\n"
		handle.write(key)
		handle.flush()
		handle.close()
		yield handle.name
	finally:
		try:
			os.unlink(handle.name)
		except OSError:
			pass


def _ensure_known_hosts_directory() -> None:
	parent = KNOWN_HOSTS_PATH.parent
	if not parent.exists():
		parent.mkdir(mode=0o700, parents=True, exist_ok=True)
