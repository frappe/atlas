"""Use case: provision a Firecracker host.

Operator clicks "Provision Server" on `Atlas Settings`. The button calls
DO to create a droplet, inserts a `Server` row, waits for SSH, runs
`bootstrap-server.py`, and parses the JSON tail-line back onto the row.

This module exercises:

- The happy path end-to-end against a fresh droplet ([run](#run)).
- Host hardening: after bootstrap (and again after an idempotent re-bootstrap)
  a probe reads back the CIS sysctl/sshd/module/update controls and asserts the
  three deliberate deviations hold — forwarding stays on, squashfs stays
  loadable, root keeps key-only login. See spec/03-bootstrapping.md.
- The validation throws that guard the same code path
  ([run_against_shared](#run_against_shared)):
  - `Provider.authenticate()` for the Server's vendor (token works or 403s cleanly).
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

from atlas.atlas import providers
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

	ensure_e2e_provider()
	title = f"atlas-e2e-fresh-{int(time.time())}"
	server_doc = None

	try:
		server_name = frappe.get_single("Atlas Settings").provision_server(title)
		server_doc = _wait_for_status(server_name, target={"Active", "Broken"}, timeout=600)
		assert server_doc.status == "Active", f"expected Active, got {server_doc.status}"
		assert server_doc.firecracker_version, "firecracker_version not recorded"
		assert server_doc.jailer_version, "jailer_version not recorded"

		bootstrap_tasks = frappe.get_all(
			"Task",
			filters={"server": server_name, "script": "bootstrap-server.py", "status": "Success"},
		)
		assert bootstrap_tasks, "no successful bootstrap Task found"

		_assert_remote_layout(server_name)
		_assert_hardening_applied(server_name)
		_assert_pool_present(server_name)

		# Idempotency: re-bootstrap on an already-Active server, then prove the
		# hardening + pool state is unchanged (drop-ins are install -m overwrites
		# and atlas_pool_ensure is gated, so a re-run is a clean no-op — both
		# readbacks must still pass).
		server_doc.bootstrap()
		_assert_hardening_applied(server_name)
		_assert_pool_present(server_name)
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


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""Host/API-only path for development against the shared server. Drives the
	DO `authenticate()` round trip and the `finish_provisioning` idempotency
	re-run (both need a live host / live token).

	The true fresh-provision host fact is `run()` — it owns a dedicated droplet
	and is the only path through `provision_server` + `finish_provisioning`
	against a fresh host. Skips the duplicate-name throw, bootstrap status
	guard, and `get_scripts` (pure logic, covered by `server/test_server.py`
	and `server/test_server_runtask.py`)."""
	with phase("server-provisioning (smoke)", reuse=reuse, keep=keep) as server:
		_check_test_connection(server)
		_check_finish_provisioning_idempotent(server)
		# The shared host was bootstrapped (and re-bootstrapped by the
		# idempotency check above), so the hardening drop-ins + thin pool are
		# present — read them back here too, the cheap host-only regression guard.
		_assert_hardening_applied(server.name)
		_assert_pool_present(server.name)


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
	assert "vm-network-up.py OK" in task.stdout
	assert "vm-network-down.py OK" in task.stdout
	assert "firecracker-vm@.service OK" in task.stdout


def _assert_hardening_applied(server_name: str) -> None:
	"""Read back the host-hardening state bootstrap-server.py applies, including
	the three deliberate deviations (forwarding stays on, squashfs stays
	loadable, root keeps key-only login). The probe is fail-loud, so a missing
	or wrong control surfaces as a non-Success Task."""
	task = run_task(
		server=server_name,
		script="phase-hardening-probe.sh",
		variables={},
		timeout_seconds=60,
	)
	assert task.status == "Success", task.stderr
	assert "HARDENING PROBE OK" in task.stdout, task.stdout


def _assert_pool_present(server_name: str) -> None:
	"""Read back the LVM thin pool bootstrap-server.py creates: dm_thin_pool
	loaded + persisted, the atlas VG and pool0 thin LV present, and the
	reboot-survival oneshot enabled. Fail-loud probe, so a missing piece
	surfaces as a non-Success Task."""
	task = run_task(
		server=server_name,
		script="phase-pool-present.sh",
		variables={},
		timeout_seconds=60,
	)
	assert task.status == "Success", task.stderr
	assert "POOL PROBE OK" in task.stdout, task.stdout


# ----- shared-server validation --------------------------------------------


def _check_test_connection(server) -> None:
	"""authenticate() returns AuthResult. Either ok=True or ok=False with
	an explanatory error message; both drive the same code path."""
	provider = providers.for_provider_type(server.provider_type)
	result = provider.authenticate()
	assert "ok" in result, result
	if not result["ok"]:
		error = result.get("error") or ""
		assert "401" in error or "403" in error or "forbidden" in error.lower(), error


def _check_provision_server_duplicate_name(server) -> None:
	caught = False
	try:
		frappe.get_single("Atlas Settings").provision_server(server.title)
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
	assert "sync-image.py" in names, names


def _check_finish_provisioning_idempotent(server) -> None:
	"""finish_provisioning normally runs in a worker. Every step is idempotent;
	re-running it against an already-Active row should leave the row Active."""
	from atlas.atlas.providers.worker import finish_provisioning

	assert server.provider_resource_id, "shared server has no provider_resource_id"
	finish_provisioning(server.name)
	server.reload()
	assert server.status == "Active", server.status
