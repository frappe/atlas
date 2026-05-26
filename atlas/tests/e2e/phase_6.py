"""Phase 6 e2e: exercise the VM lifecycle (start/stop/restart/delete)."""

import time

import frappe

from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_public_key,
	expect_validation_error,
	phase,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	with phase("phase-6", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		image = image_doc.name

		public_key = ephemeral_public_key()

		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"description": "phase 6 e2e",
			"server": server.name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": public_key,
		}).insert(ignore_permissions=True)

		vm.provision()
		vm.reload()
		assert vm.status == "Running", vm.status
		first_started = vm.last_started
		assert_probe(server.name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)

		# Stop
		vm.stop()
		vm.reload()
		assert vm.status == "Stopped", vm.status
		assert vm.last_stopped, "last_stopped should be set"
		assert_probe(server.name, "phase6-is-inactive.sh", VIRTUAL_MACHINE_NAME=vm.name)

		# Start
		time.sleep(1)  # advance clock for last_started comparison
		vm.start()
		vm.reload()
		assert vm.status == "Running", vm.status
		assert vm.last_started > first_started, (
			f"last_started did not advance: {first_started} -> {vm.last_started}"
		)
		assert_probe(server.name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)

		# Restart (Running -> Running, two tasks)
		before_stop = vm.last_stopped
		before_start = vm.last_started
		time.sleep(1)
		result = vm.restart()
		assert result["stop_task"] and result["start_task"], result
		vm.reload()
		assert vm.status == "Running", vm.status
		assert vm.last_stopped > before_stop, "last_stopped did not advance on restart"
		assert vm.last_started > before_start, "last_started did not advance on restart"
		assert_probe(server.name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)

		# Terminate
		tap_device = vm.tap_device
		vm.terminate()
		vm.reload()
		assert vm.status == "Terminated", vm.status
		assert_probe(
			server.name,
			"phase6-assert-gone.sh",
			VIRTUAL_MACHINE_NAME=vm.name,
			TAP_DEVICE=tap_device,
		)

		# Terminate again -> raises
		with expect_validation_error("already terminated"):
			vm.terminate()
