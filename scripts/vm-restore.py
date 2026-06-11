#!/usr/bin/env python3
# Resume a VM from its pending memory snapshot. Invoked by ExecStartPost in the
# systemd unit, after the jailer launched Firecracker. Two cases:
#
# - No marker (the common cold boot): Firecracker already booted the guest from
#   firecracker.json (the launcher passed --config-file). Exit 0, no-op.
# - Marker present (snapshot-stop-vm.py left a complete vmstate + RAM pair):
#   the launcher started Firecracker IDLE (no --config-file, /snapshot/load is
#   pre-boot only), so load the snapshot, CONSUME the marker, then resume. The
#   guest is back at its pre-stop instruction in milliseconds instead of a
#   60-120s cold boot.
#
# The marker is consumed BEFORE the guest runs again: once resumed, the guest
# writes to its disk and the saved RAM no longer matches it, so the same
# snapshot must never be loaded twice. Any failure here removes the marker and
# exits non-zero — the unit fails, Restart=always relaunches it 5s later, the
# launcher sees no marker, and the VM cold-boots from firecracker.json. The
# fast path degrades to the default path, never to a wedged unit.
#
# systemd-invoked, NOT a Task: positional uuid (`%i`), durable atlas package
# under /var/lib/atlas/bin — same shape as vm-disk-up.py / vm-network-up.py.

import json
import os
import sys
import time

# The durable package lives next to this script under /var/lib/atlas/bin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atlas._run import CommandError, firecracker_api
from atlas.paths import VirtualMachinePaths

# Firecracker creates the API socket within milliseconds of exec; 10s is a
# generous ceiling before declaring the launch dead.
SOCKET_WAIT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05


def main() -> None:
	if len(sys.argv) != 2:
		sys.exit("usage: vm-restore.py <virtual-machine-uuid>")
	uuid = sys.argv[1]
	paths = VirtualMachinePaths(uuid)

	if not os.path.exists(paths.memory_snapshot_marker):
		return  # cold boot: --config-file already booted the guest

	try:
		_wait_for_socket(paths.api_socket)
		# Load paused (resume_vm false) so the marker can be consumed strictly
		# before the guest runs — the crash-anywhere outcome is then either
		# "marker still present, disk untouched, retry restores safely" or
		# "marker gone, next start cold-boots"; never a double-restore.
		_load_snapshot(paths)
	except (CommandError, TimeoutError) as error:
		os.remove(paths.memory_snapshot_marker)
		sys.exit(f"memory-snapshot restore failed ({error}); marker consumed, next start cold-boots")
	os.remove(paths.memory_snapshot_marker)
	firecracker_api(
		paths.api_socket_directory,
		paths.api_socket_name,
		"PATCH",
		"/vm",
		'{"state": "Resumed"}',
	)
	print(f"Restored {uuid} from memory snapshot.")


def _wait_for_socket(socket_path: str) -> None:
	deadline = time.monotonic() + SOCKET_WAIT_SECONDS
	while not os.path.exists(socket_path):
		if time.monotonic() > deadline:
			raise TimeoutError(f"API socket {socket_path} did not appear within {SOCKET_WAIT_SECONDS}s")
		time.sleep(POLL_INTERVAL_SECONDS)


def _load_snapshot(paths: VirtualMachinePaths) -> None:
	"""PUT /snapshot/load with the jail-relative snapshot pair. The socket file
	can exist a beat before Firecracker accepts connections, so connection-level
	failures retry briefly; a genuine rejection exhausts the retries and raises."""
	body = json.dumps(
		{
			"snapshot_path": paths.memory_snapshot_vmstate_in_jail,
			"mem_backend": {
				"backend_type": "File",
				"backend_path": paths.memory_snapshot_mem_in_jail,
			},
			"resume_vm": False,
		}
	)
	attempts = 5
	for attempt in range(attempts):
		try:
			firecracker_api(paths.api_socket_directory, paths.api_socket_name, "PUT", "/snapshot/load", body)
			return
		except CommandError:
			if attempt == attempts - 1:
				raise
			time.sleep(0.1)


if __name__ == "__main__":
	main()
