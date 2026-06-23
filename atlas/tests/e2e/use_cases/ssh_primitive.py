"""Use case: run a script over SSH against a host.

Atlas's lowest-level primitive is `run_task(connection=..., script=..., variables=...)`,
which uploads a script over scp and runs it over ssh. It is what
`Server.bootstrap()` uses before a `Server` row's provider linkage is usable.

[run_against_shared](#run_against_shared) exercises `upload_files`,
`wait_for_ssh`, `run_scp`, and `Server.bootstrap()` re-run against the
shared bootstrapped server used by `run_all`. The happy/failure/missing-script
paths of `run_task` itself live in the `run_task` use case.

Also folds in the argument-validation throws that guard the entry point
(`run_task` requires exactly one of `server=` or `connection=`,
`connection_for_server` requires `ipv4_address`, `execute_task` requires
a Task with a `server`). These do not require a droplet at all; they
ride along on this entry point.
"""

import frappe

from atlas.atlas._ssh.runner import connection_for_server, execute_task, run_task
from atlas.atlas._ssh.transport import (
	Connection,
	run_scp,
	ssh_key_file,
	upload_files,
	wait_for_ssh,
)
from atlas.atlas.ssh import connection_for_server as public_connection_for_server
from atlas.tests.e2e._shared import expect_validation_error, phase


def run_against_shared(reuse: bool = True, keep: bool = True) -> None:
	"""Shared-droplet path. Drives upload_files / wait_for_ssh / run_scp
	failure / Server.bootstrap() re-run. Used by run_all so the transport
	branches are exercised on a real bootstrapped host."""
	with phase("ssh-primitive (transport+bootstrap)", reuse=reuse, keep=keep) as server:
		connection = public_connection_for_server(server)
		_check_upload_files_happy(connection)
		_check_upload_files_empty(connection)
		_check_scp_failure(connection)
		_check_wait_for_ssh_timeout()
		_check_server_bootstrap_rerun(server)
		_check_run_task_argument_validation(connection)
		_check_connection_for_server_validation()
		_check_execute_task_no_server()


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""Host-only path for development. Drives the transport primitives against
	the live host: a real scp upload, the non-zero-returncode scp branch, and
	a full `Server.bootstrap()` re-run.

	Skips the argument-validation throws, `connection_for_server` no-ipv4,
	`execute_task` no-server, and the `wait_for_ssh` unroutable-host timeout —
	all pure logic, covered by `test_ssh_runner.py` and
	`test_ssh_transport.py`."""
	with phase("ssh-primitive (smoke)", reuse=reuse, keep=keep) as server:
		connection = public_connection_for_server(server)
		_check_upload_files_happy(connection)
		_check_scp_failure(connection)
		_check_server_bootstrap_rerun(server)


def _check_run_task_argument_validation(connection: Connection) -> None:
	"""run_task requires exactly one of server= or connection=."""
	with expect_validation_error("exactly one"):
		run_task(server="any", connection=connection, script="x.sh", variables={})
	with expect_validation_error("exactly one"):
		run_task(script="x.sh", variables={})


# ----- shared droplet (run_against_shared) ---------------------------------


def _check_upload_files_happy(connection: Connection) -> None:
	import tempfile

	with tempfile.NamedTemporaryFile(mode="w", suffix=".usecase", delete=False) as handle:
		handle.write("ssh-primitive marker\n")
		local_path = handle.name
	upload_files(connection, [(local_path, "/tmp/atlas-usecase-marker.txt")])


def _check_upload_files_empty(connection: Connection) -> None:
	"""upload_files([]) returns silently."""
	upload_files(connection, [])


def _check_scp_failure(connection: Connection) -> None:
	"""scp into /proc fails, driving the non-zero-returncode branch."""
	import tempfile

	with tempfile.NamedTemporaryFile(mode="w", suffix=".usecase", delete=False) as handle:
		handle.write("x\n")
		local_path = handle.name

	caught = False
	try:
		with ssh_key_file(connection.ssh_private_key) as key_path:
			run_scp(connection, key_path, local_path, "/proc/atlas-usecase/x", timeout_seconds=30)
	except frappe.ValidationError:
		caught = True
	assert caught, "scp to /proc should have raised ValidationError"


def _check_wait_for_ssh_timeout() -> None:
	"""wait_for_ssh against TEST-NET-1 raises within the deadline."""
	connection = Connection(
		host="192.0.2.1",
		ssh_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n",
	)
	caught = False
	try:
		wait_for_ssh(connection, timeout_seconds=2, poll_seconds=1)
	except frappe.ValidationError as exception:
		caught = "not ready" in str(exception).lower()
	except Exception:
		caught = True
	assert caught, "wait_for_ssh against unroutable host should raise"


def _check_server_bootstrap_rerun(server) -> None:
	"""Re-run Server.bootstrap() on the already-Active server. Drives
	`upload_files`, `_bootstrap_uploads`, `_absorb_bootstrap_output`, and the
	JSON tail-line parser — none of which `run_task_dialog` reaches."""
	original_firecracker = server.firecracker_version
	server.bootstrap()
	server.reload()
	assert server.firecracker_version == original_firecracker, (
		server.firecracker_version,
		original_firecracker,
	)


def _check_connection_for_server_validation() -> None:
	"""connection_for_server requires ipv4_address. The SSH key path now lives
	on Atlas Settings (not Server.provider_type), so the legacy "no provider"
	guard is gone."""
	transient = frappe.get_doc(
		{
			"doctype": "Server",
			"title": "usecase-no-ip",
			"status": "Pending",
		}
	)
	with expect_validation_error("no ipv4_address"):
		connection_for_server(transient)


def _check_execute_task_no_server() -> None:
	"""execute_task on a Task with no server attribute throws."""
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"script": "noop.sh",
			"status": "Pending",
			"triggered_by": "Administrator",
		}
	)
	task.variables_dict = {}
	task.insert(ignore_permissions=True)
	frappe.db.commit()
	with expect_validation_error("no server"):
		execute_task(task.name)
