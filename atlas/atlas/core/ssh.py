"""Public SSH surface for Atlas.

The implementation lives in `atlas.atlas.core._ssh.{runner,transport}`. This module
re-exports the symbols every caller (controllers, e2e, tests) imports, so the
import path `from atlas.atlas.core.ssh import ...` stays stable.
"""

from atlas.atlas.core._ssh.runner import (
	connection_for_guest,
	connection_for_server,
	execute_task,
	run_probe,
	run_task,
)
from atlas.atlas.core._ssh.transport import (
	KNOWN_HOSTS_PATH,
	REMOTE_STAGING_DIRECTORY,
	SSH_OPTIONS,
	Connection,
	forget_host,
	run_ssh,
	ssh_key_file,
	upload_files,
	wait_for_ssh,
)

__all__ = [
	"KNOWN_HOSTS_PATH",
	"REMOTE_STAGING_DIRECTORY",
	"SSH_OPTIONS",
	"Connection",
	"connection_for_guest",
	"connection_for_server",
	"execute_task",
	"forget_host",
	"run_probe",
	"run_ssh",
	"run_task",
	"ssh_key_file",
	"upload_files",
	"wait_for_ssh",
]
