"""Canonical Virtual Machine size presets.

One source of truth for the size ladder the dashboard's New Machine dialog and
the desk form offer. A preset is a convenience that fans out to the VM's four
resource fields; it is not stored as anything but the resource values it sets
(the `size_preset` Select on Virtual Machine records which preset was picked,
purely for display).

The vCPU ladder is `1/16, 1/8, 1/4, 1/2, 1, 2, 4`. Sub-1 sizes are honest CPU
*bandwidth* caps, not whole cores:

- `vcpus` is the guest's `vcpu_count` — the number of vCPU threads Firecracker
  boots. It must be an integer >= 1 (a guest cannot boot on a fractional thread
  count), and it is what server-capacity accounting sums for the thread budget.
- `cpu_max_cores` is the cgroup `cpu.max` bandwidth cap in whole-core units
  (`networking.cgroup_args`). A 1/16 size is `vcpus=1, cpu_max_cores=0.0625`:
  the guest sees one vCPU thread, host-throttled to 6.25% of a core.

The two whole-core+ sizes (2 and 4 vCPU) set `cpu_max_cores == vcpus`, so they
behave exactly as before this field existed.

`atlas/atlas/doctype/virtual_machine/virtual_machine.js` carries matching
literals (JS can't import Python); `atlas/tests/test_sizes.py` pins them in sync
with this module and with the doctype JSON's `size_preset` options.
"""

# Ordered: smallest first. The key is the human label shown in the Select and
# is also the value stored in Virtual Machine.size_preset.
#
# The "Shared Nx" tiers are oversubscribable fractions of a core (cpu.max
# bandwidth below one core); "Shared 1x" is the base unit (1/16 of a core) and
# 2x/4x/8x are multiples of it. "Dedicated 1x" is a full guaranteed core
# (cpu_max_cores = 1). Memory doubles from 512 MB, disk doubles from 10 GB.
SIZE_PRESETS: dict[str, dict[str, float | int]] = {
	"Shared 1x": {
		"vcpus": 1,
		"cpu_max_cores": 0.0625,
		"memory_megabytes": 512,
		"disk_gigabytes": 10,
	},
	"Shared 2x": {
		"vcpus": 1,
		"cpu_max_cores": 0.125,
		"memory_megabytes": 1024,
		"disk_gigabytes": 20,
	},
	"Shared 4x": {
		"vcpus": 1,
		"cpu_max_cores": 0.25,
		"memory_megabytes": 2048,
		"disk_gigabytes": 40,
	},
	"Shared 8x": {
		"vcpus": 1,
		"cpu_max_cores": 0.5,
		"memory_megabytes": 4096,
		"disk_gigabytes": 80,
	},
	"Dedicated 1x": {
		"vcpus": 1,
		"cpu_max_cores": 1,
		"memory_megabytes": 8192,
		"disk_gigabytes": 160,
	},
}


def size_preset_options() -> str:
	"""The Virtual Machine.size_preset Select `options` string.

	`Custom` first (the doctype default for the operator who types raw resource
	values), then the five tiers in ladder order. Newline-joined, the Frappe
	Select-options format. Kept here so the doctype JSON and the desk
	picker are regenerated from one list."""
	return "\n".join(["Custom", *SIZE_PRESETS.keys()])
