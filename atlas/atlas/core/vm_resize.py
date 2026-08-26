"""Resizing a Stopped VM — changing its vCPU / CPU bandwidth / memory / disk and
growing the rootfs to match (spec/05-vm-lifecycle, spec/28 capacity gate).

Extracted from the `Virtual Machine` controller: reshaping a VM's machine spec is
one cohesive reason to change (the sizing + capacity-accounting rules), separate
from the create/power/terminate lifecycle and from the disk-image operations in
`vm_images.py`. Free functions taking the VM, per the `vm_provisioning.py` /
`migration.py` pattern. The controller keeps a thin `@whitelist resize` delegator
(the Central/desk RPC surface + external Python callers).
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.core import vm_provisioning
from atlas.atlas.core.networking import cgroup_args
from atlas.atlas.core.placement import check_resize_capacity


def resize(
	vm,
	vcpus: int | None = None,
	cpu_max_cores: float | None = None,
	cpu_mode: str | None = None,
	memory_megabytes: int | None = None,
	disk_gigabytes: int | None = None,
	data_disk_gigabytes: int | None = None,
) -> str:
	"""Change vCPU / CPU bandwidth / memory / disk on a Stopped VM.

	Firecracker can't resize a running VM (machine-config is pre-boot
	only), so the operator stops first. Disk may only grow — ext4 shrink
	is unsafe and the on-host rootfs is already that large. The new values
	are persisted, then resize-vm.py rewrites the firecracker config and
	grows the rootfs to match. The VM stays Stopped.

	`cpu_max_cores` is the VM's guaranteed CPU bandwidth share and `cpu_mode`
	is how it is enforced (hard cgroup cpu.max ceiling vs. cpu.weight floor +
	burst). resize-vm.py rewrites firecracker.json (vcpu_count/mem), grows the
	disk, AND splices the new cgroup caps (CGROUP_ARG below) into the per-VM
	jailer launcher — so the new memory.max / cpu.max take effect on the next
	Start. The launcher rewrite is load-bearing for memory: firecracker.json's
	guest RAM and the launcher's `memory.max` are independent ceilings, and a
	stale memory.max caps the guest below its new RAM → CONSTRAINT_MEMCG
	OOM-kill on first boot (the exact failure this once had before CGROUP_ARG
	was forwarded). When the caller changes vcpus but leaves cpu_max_cores
	unset, keep the share in step for a whole-core VM (share == old vcpus);
	otherwise the explicit share (or the unchanged fractional one) stands.
	cpu_mode is left untouched unless passed."""
	if vm.status != "Stopped":
		frappe.throw(f"Stop the VM before resizing (status is {vm.status})")
	vm._guard_no_active_migration()
	new_vcpus = int(vcpus) if vcpus else vm.vcpus
	new_memory = int(memory_megabytes) if memory_megabytes else vm.memory_megabytes
	new_disk = int(disk_gigabytes) if disk_gigabytes else vm.disk_gigabytes
	new_data_disk = int(data_disk_gigabytes) if data_disk_gigabytes else vm.data_disk_gigabytes
	new_cpu_max = resolve_resize_cpu_max(vm, cpu_max_cores, new_vcpus)
	new_cpu_mode = cpu_mode or vm.cpu_mode
	if new_disk < vm.disk_gigabytes:
		frappe.throw(f"Disk can only grow: {vm.disk_gigabytes} GB → {new_disk} GB is a shrink")
	# The data disk grows like the root disk, with one extra rule: resize only
	# GROWS an existing data disk. Adding one to a VM that never had one would
	# also need a new Firecracker drive + fstab line (a re-provision concern),
	# so that path is recreate-the-VM, not resize.
	if new_data_disk != vm.data_disk_gigabytes:
		if not vm.data_disk_gigabytes:
			# fmt: off
			frappe.throw(_("This VM has no data disk; recreate the VM to add one (resize only grows an existing data disk)"))
			# fmt: on
		if new_data_disk < vm.data_disk_gigabytes:
			frappe.throw(
				f"Data disk can only grow: {vm.data_disk_gigabytes} GB → {new_data_disk} GB is a shrink"
			)
	# Capacity gate (spec/28): a resize must not silently oversubscribe the host.
	# Charge only the positive per-axis deltas against the host's FULL effective
	# budget — the arrival headroom reserve is the resize's to spend. Raises
	# NoResizeCapacityError (a NoCapacityError subclass) when the delta doesn't
	# fit; that is the trigger for a future migrate-to-grow (case 2). CPU cost is
	# the bandwidth share (cpu_max_cores or vcpus), matching capacity accounting.
	check_resize_capacity(
		vm.server,
		delta_cpu=new_cpu_max - float(vm.cpu_max_cores or vm.vcpus or 0),
		delta_memory_mb=new_memory - (vm.memory_megabytes or 0),
		delta_disk_gb=(new_disk + new_data_disk) - (vm.disk_gigabytes + (vm.data_disk_gigabytes or 0)),
	)
	# Run the on-host resize first; run_task raises on failure, so we only
	# persist the new values once the config and disk actually changed.
	# Saving before the Task would let a failed resize-vm.py leave the doc
	# claiming a size the host never applied — the exact drift the freeze
	# guards against.
	variables = {
		"VIRTUAL_MACHINE_NAME": vm.name,
		"VCPUS": str(new_vcpus),
		"MEMORY_MB": str(new_memory),
		"DISK_GB": str(new_disk),
		# The new jailer cgroup caps, derived from the resized memory/cpu exactly
		# as provision does. resize-vm.py splices these into jailer-launch.sh so
		# the host cgroup memory.max tracks the new RAM — without it the launcher
		# pins the pre-resize cap and the guest OOM-kills on the RAM it was given.
		"CGROUP_ARG": vm_provisioning.cgroup_values(
			cgroup_args(new_cpu_max, new_memory, new_disk, new_cpu_mode, new_vcpus)
		),
	}
	if new_data_disk:
		variables["DATA_DISK_GB"] = str(new_data_disk)
		variables["DATA_DISK_FORMAT"] = "1" if vm.data_disk_format_and_mount else "0"
	# Boat's resize carries no numbers — it applies the desired ones — so the
	# new spec is stated before the verb is asked to apply it. The row is
	# still written only after the host confirms, exactly as below: what is
	# desired the moment the operator asks for it, and what is true, are two
	# different facts (spec/33 §1).
	run = vm._transport(
		vcpus=new_vcpus,
		cpu_max_cores=new_cpu_max,
		cpu_mode=new_cpu_mode,
		memory_megabytes=new_memory,
		disk_gigabytes=new_disk,
		data_disk_gigabytes=new_data_disk,
	)
	task = run(
		server=vm.server,
		script="resize-vm",
		variables=variables,
		virtual_machine=vm.name,
		timeout_seconds=120,
	)
	vm.vcpus = new_vcpus
	vm.cpu_max_cores = new_cpu_max
	vm.cpu_mode = new_cpu_mode
	vm.memory_megabytes = new_memory
	vm.disk_gigabytes = new_disk
	vm.data_disk_gigabytes = new_data_disk
	# resize-vm.py dropped any pending memory snapshot (the saved vmstate no
	# longer matches the new machine config); mirror that on the row.
	vm.has_memory_snapshot = 0
	vm.flags.resizing = True
	vm.save()
	return task.name


def resolve_resize_cpu_max(vm, cpu_max_cores: float | None, new_vcpus: int) -> float:
	"""The cpu_max_cores to persist on a resize.

	An explicit value wins. Otherwise, when the VM was whole-core (cap ==
	current vcpus) and the resize changes vcpus, track the new vcpus so a
	whole-core VM stays whole-core. A fractional VM (cap != vcpus) keeps its
	cap untouched unless the caller passes a new one."""
	if cpu_max_cores:
		return float(cpu_max_cores)
	if vm.cpu_max_cores == float(vm.vcpus):
		return float(new_vcpus)
	return float(vm.cpu_max_cores)
