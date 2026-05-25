"""Phase 7 e2e: Run Task dialog + Reboot button.

Reuses a bootstrapped server. Exercises:

1. run_task_dialog with a real script (idempotent re-bootstrap).
2. run_task_dialog with an unknown script raises.
3. reboot kicks SSH; server comes back; probe succeeds afterwards.
"""

import time
import traceback

import frappe

from atlas.tests.e2e._shared import (
	cleanup_droplet,
	ensure_bootstrapped_server,
	server_is_reachable,
	sweep_old_droplets,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	start_clock = time.monotonic()
	server, client, created_now = ensure_bootstrapped_server(reuse=reuse, keep=keep)
	sweep_old_droplets(client)

	try:
		# 1. Re-bootstrap via run_task_dialog (same code path as bootstrap()).
		task_name = server.run_task_dialog(
			script="bootstrap-server.sh",
			variables={
				"FIRECRACKER_VERSION": "v1.15.1",
				"ARCHITECTURE": "x86_64",
			},
		)
		task = frappe.get_doc("Task", task_name)
		assert task.status == "Success", task.stderr
		assert task.script == "bootstrap-server.sh"
		assert task.server == server.name

		# 2. Unknown script must raise.
		raised = False
		try:
			server.run_task_dialog(script="nope.sh", variables={})
		except frappe.ValidationError:
			raised = True
		assert raised, "run_task_dialog should reject unknown scripts"

		# 3. Reboot. SSH drops mid-Task -> Failure. Then probe until server is back.
		reboot_task_name = server.reboot()
		reboot_task = frappe.get_doc("Task", reboot_task_name)
		assert reboot_task.script == "reboot-server.sh"
		# Task likely ended in Failure (SSH drop). Either Failure or Success
		# is acceptable; we care that the server comes back.
		assert reboot_task.status in ("Failure", "Success"), reboot_task.status

		# Give the box time to actually go down before checking it comes back,
		# else the probe will see the still-alive SSH from before the reboot.
		print("[phase-7] waiting 30s for server to begin reboot...")
		time.sleep(30)

		print("[phase-7] probing server until SSH responds (up to 5 min)...")
		deadline = time.monotonic() + 300
		while time.monotonic() < deadline:
			if server_is_reachable(server.name, timeout_seconds=10):
				break
			time.sleep(5)
		else:
			raise AssertionError("server did not come back within 5 minutes")
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"phase-7: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	finally:
		if created_now and not keep and server.provider_resource_id:
			cleanup_droplet(client, int(server.provider_resource_id))

	elapsed = time.monotonic() - start_clock
	print(f"phase-7: OK in {elapsed:.0f}s")
