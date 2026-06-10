#!/usr/bin/env python3
# Resize a Stopped VM: set vCPU/memory in its firecracker config and grow the
# rootfs to DISK_GB. Firecracker reads machine-config only at boot, so the VM
# must be Stopped — the next Start picks up the new config. Disk only grows
# (the caller rejects shrink). Idempotent: re-running writes the same values
# and resize2fs is a no-op once the filesystem already fills the device.
#
# Successor to resize-vm.sh. Inputs are now typed CLI flags parsed once by
# ResizeInputs.from_args() (vcpus/memory-mb/disk-gb declared `int` so argparse
# coerces and rejects non-integers). No machine-readable result — the controller
# reads nothing back, so we print only the human "Resized ..." line.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_file, run
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class ResizeInputs(TaskInputs):
	"""Resize a Stopped VM's vCPU/memory (next boot) and grow its rootfs."""

	command: typing.ClassVar[str] = "resize-vm"
	virtual_machine_name: str  # UUID; locates the VM directory and disk LV
	vcpus: int
	memory_mb: int
	disk_gb: int  # target rootfs size; grow-only
	# Data disk (the root disk's peer), grow-only. 0 = the VM has no data disk.
	# data_disk_format is an int (0/1), not a bool — see provision-vm.py.
	data_disk_gb: int = 0
	data_disk_format: int = 1


def main() -> None:
	inputs = ResizeInputs.from_args()
	pool = ThinPool()

	# Config lives inside the VM's jail; the disk is the VM's LV.
	paths = VirtualMachinePaths(inputs.virtual_machine_name)
	config_path = paths.firecracker_config
	disk = pool.vm_disk(inputs.virtual_machine_name)

	if not os.path.isfile(config_path):
		sys.exit(f"firecracker config {config_path} missing; provision the VM first")
	if not disk.exists:
		sys.exit(f"disk LV {disk.name} missing; provision the VM first")

	# 1. Rewrite machine-config in place. jq edits only the two keys, preserving
	#    boot-source, drives and network-interfaces. The replacement file is created
	#    by root; copy the original's owner onto it so the jailed Firecracker (the
	#    per-VM uid) can still read its config after chroot.
	new_config = run(
		"sudo",
		"jq",
		"--argjson",
		"vcpus",
		str(inputs.vcpus),
		"--argjson",
		"mem",
		str(inputs.memory_mb),
		'."machine-config".vcpu_count = $vcpus | ."machine-config".mem_size_mib = $mem',
		config_path,
	)
	install_file(new_config, f"{config_path}.new", mode="0644")
	run("sudo", "chown", f"--reference={config_path}", f"{config_path}.new")
	run("sudo", "mv", f"{config_path}.new", config_path)

	# 2. Grow the disk LV to DISK_GB. lvextend -r extends the LV and the ext4 on it
	#    in one shot. Disk only ever grows (shrink is rejected upstream); lvextend
	#    refuses to shrink and is a clean no-op when the LV already meets the size,
	#    so a re-run (or a resize that only changed vCPU/memory) does not error.
	run("sudo", "lvextend", "-r", "-L", f"{inputs.disk_gb}G", disk.device_path, check=False, quiet=True)

	# 3. Grow the data disk (the root disk's peer) when present. Grow-only, same as
	#    root: -r resizes the ext4 too when formatted, else the block device only.
	#    Skipped when the VM has no data disk; a clean no-op when the LV already
	#    meets the size (idempotent re-run, or a resize that only touched vCPU/mem).
	#    The controller rejects 0→N, so a data_disk_gb here always has a live LV.
	if inputs.data_disk_gb > 0:
		data_disk = pool.data_disk(inputs.virtual_machine_name)
		if data_disk.exists:
			grow = ["sudo", "lvextend"]
			grow += ["-r"] if inputs.data_disk_format else []
			grow += ["-L", f"{inputs.data_disk_gb}G", data_disk.device_path]
			run(*grow, check=False, quiet=True)

	print(
		f"Resized {inputs.virtual_machine_name}: "
		f"{inputs.vcpus} vCPU, {inputs.memory_mb} MB, {inputs.disk_gb} GB"
		+ (f", data {inputs.data_disk_gb} GB." if inputs.data_disk_gb > 0 else ".")
	)


if __name__ == "__main__":
	main()
