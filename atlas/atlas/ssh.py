"""Public SSH surface for Atlas.

The implementation lives in `atlas.atlas._ssh.{runner,transport}`. This module
re-exports the symbols every caller (controllers, e2e, tests) imports, so the
import path `from atlas.atlas.ssh import ...` stays stable.
"""

from atlas.atlas._ssh.runner import (
	connection_for_server,
	execute_task,
	run_task,
)
from atlas.atlas._ssh.transport import (
	KNOWN_HOSTS_PATH,
	REMOTE_STAGING_DIRECTORY,
	SSH_OPTIONS,
	Connection,
	upload_files,
	wait_for_ssh,
)

__all__ = [
	"KNOWN_HOSTS_PATH",
	"REMOTE_STAGING_DIRECTORY",
	"SSH_OPTIONS",
	"Connection",
	"connection_for_server",
	"execute_task",
	"run_task",
	"upload_files",
	"wait_for_ssh",
]
