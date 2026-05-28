"""Use case: provision a Firecracker host.

Operator clicks "Provision Server" on a `Server Provider`. The button calls
DO to create a droplet, inserts a `Server` row, waits for SSH, runs
`bootstrap-server.sh`, and parses the JSON tail-line back onto the row.

This module exercises:

- The happy path end-to-end against a fresh droplet ([run](#run)).
- The validation throws that guard the same code path
  ([run_against_shared](#run_against_shared)):
  - `Server Provider.test_connection()` (token works or 403s cleanly).
  - `provision_server` rejects a duplicate name.
  - `Server.bootstrap()` from a non-Active status throws.
  - `Server.get_scripts()` returns the catalogue.
  - `finish_provisioning(...)` is idempotent — sync-callable against an
    already-Active row without regressing it.

The fresh-provision path needs its own droplet (that's the thing being
tested); the validation path piggybacks on the shared bootstrapped server.
"""

import time
import traceback

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._shared import (
	cleanup_droplet,
	ensure_e2e_provider,
	expect_validation_error,
	get_client,
	phase,
	sweep_old_droplets,
)


def run() -> None:
	"""Fresh-provision happy path. Creates and tears down its own droplet."""
	start_clock = time.monotonic()
	client = get_client()
	sweep_old_droplets(client)

	provider = ensure_e2e_provider()
	title = f"atlas-e2e-fresh-{int(time.time())}"
	server_doc = None

	try:
		server_name = provider.provision_server(title)
		server_doc = _wait_for_status(server_name, target={"Active", "Broken"}, timeout=600)
		assert server_doc.status == "Active", f"expected Active, got {server_doc.status}"
		assert server_doc.firecracker_version, "firecracker_version not recorded"

		bootstrap_tasks = frappe.get_all(
			"Task",
			filters={"server": server_name, "script": "bootstrap-server.sh", "status": "Success"},
		)
		assert bootstrap_tasks, "no successful bootstrap Task found"

		_assert_remote_layout(server_name)

		# Idempotency: re-bootstrap on an already-Active server.
		server_doc.bootstrap()
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"server-provisioning: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	finally:
		if server_doc and server_doc.provider_resource_id:
			cleanup_droplet(client, int(server_doc.provider_resource_id))

	elapsed = time.monotonic() - start_clock
	print(f"server-provisioning: OK in {elapsed:.0f}s")


def run_against_shared(reuse: bool = True, keep: bool = True) -> None:
	"""Validation-and-idempotency checks that reuse the shared server."""
	with phase("server-provisioning (validation+idempotency)", reuse=reuse, keep=keep) as server:
		_check_test_connection(server)
		_check_provision_server_duplicate_name(server)
		_check_bootstrap_status_guard(server)
		_check_get_scripts(server)
		_check_finish_provisioning_idempotent(server)


# ----- fresh-provision helpers ---------------------------------------------


def _wait_for_status(server_name: str, target: set[str], timeout: int):
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		frappe.db.rollback()
		server = frappe.get_doc("Server", server_name)
		if server.status in target:
			return server
		time.sleep(5)
	raise AssertionError(f"server {server_name} did not reach {target} within {timeout}s")


def _assert_remote_layout(server_name: str) -> None:
	task = run_task(
		server=server_name,
		script="phase3-probe.sh",
		variables={},
		timeout_seconds=30,
	)
	assert task.status == "Success"
	assert "vm-network-up.sh OK" in task.stdout
	assert "vm-network-down.sh OK" in task.stdout
	assert "firecracker-vm@.service OK" in task.stdout


# ----- shared-server validation --------------------------------------------


def _check_test_connection(server) -> None:
	"""test_connection returns ok or DigitalOceanError (403). Either drives
	the same code path."""
	from atlas.atlas.digitalocean import DigitalOceanError

	provider = frappe.get_doc("Server Provider", server.provider)
	try:
		result = provider.test_connection()
		assert result.get("ok") is True, result
	except DigitalOceanError as exception:
		assert "403" in str(exception) or "forbidden" in str(exception).lower(), str(exception)


def _check_provision_server_duplicate_name(server) -> None:
	provider = frappe.get_doc("Server Provider", server.provider)
	caught = False
	try:
		provider.provision_server(server.title)
	except frappe.ValidationError as exception:
		caught = "already exists" in str(exception).lower()
	assert caught, "provision_server with duplicate title should have raised"


def _check_bootstrap_status_guard(server) -> None:
	"""Active is allowed (covered by run()); force in-memory Archived to drive
	the throw without flipping the row."""
	server.reload()
	original_status = server.status
	server.status = "Archived"
	with expect_validation_error("cannot bootstrap"):
		server.bootstrap()
	server.status = original_status


def _check_get_scripts(server) -> None:
	scripts = server.get_scripts()
	assert isinstance(scripts, list) and scripts, scripts
	names = {entry["name"] for entry in scripts}
	assert "sync-image.sh" in names, names


def _check_finish_provisioning_idempotent(server) -> None:
	"""finish_provisioning normally runs in a worker. Every step is idempotent;
	re-running it against an already-Active row should leave the row Active."""
	from atlas.atlas.doctype.server_provider.server_provider import finish_provisioning

	assert server.provider_resource_id, "shared server has no provider_resource_id"
	finish_provisioning(server.name)
	server.reload()
	assert server.status == "Active", server.status
