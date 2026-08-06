#!/usr/bin/env python3
# Regenerate a Stopped VM's SSH host keys — rotate its SSH identity on demand.
# Mounts the VM's root LV on the host, replaces /etc/ssh/ssh_host_* with fresh
# per-VM keys, unmounts. The VM stays Stopped; the new keys take effect on the
# next Start. Clients will see a changed host key and must refresh known_hosts —
# that is the point of asking for this.
#
# This is the explicit, opt-in counterpart to the preserve-by-default identity
# injection: provision establishes host keys at birth, rebuild/restore PRESERVE
# them, and this script is the only Task that deliberately changes them.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import VirtualMachinePaths
from atlas.rootfs import regenerate_host_keys_on_device


@dataclass(frozen=True)
class RegenerateHostKeysInputs(TaskInputs):
	"""Rotate a Stopped VM's SSH host keys."""

	command: typing.ClassVar[str] = "regenerate-host-keys-vm"
	virtual_machine_name: str  # UUID; locates the root disk LV and seeds the key comment


def main() -> None:
	inputs = RegenerateHostKeysInputs.from_args()
	pool = ThinPool()
	disk = pool.vm_disk(inputs.virtual_machine_name)

	if not disk.exists:
		sys.exit(f"disk LV {disk.name} missing; provision the VM first")

	# The rootfs is about to change under any pending memory snapshot; saved RAM
	# referencing the old disk must never be restored over the new one.
	run("sudo rm -rf {}", VirtualMachinePaths(inputs.virtual_machine_name).memory_snapshot_directory)

	# Activate (idempotent) before mounting — a Stopped VM's thin snapshot LV may
	# be deactivated. The key comment mirrors the per-VM hostname (atlas-<uuid8>),
	# the same value inject_identity uses; it is cosmetic (the `-C` on the key).
	disk.activate()
	hostname = f"atlas-{inputs.virtual_machine_name[:8]}"
	regenerate_host_keys_on_device(disk.device_path, hostname)

	print(f"Regenerated SSH host keys for {inputs.virtual_machine_name}.")


if __name__ == "__main__":
	main()
