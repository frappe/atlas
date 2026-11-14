# Host hardware degraded

A part of the host is failing. The alert key tells which:

- `ecc-uncorrected` (High): memory had an error that ECC could not correct. A VM or the host can crash or corrupt data.
- `disk:<device>` (Medium): SMART reports that the disk fails.
- `cpu-temperature` (Medium): the CPU is over 85 °C for 15 minutes.
- `ecc-corrected` (10 or more in 1 h), `cpu-throttle` (100 or more in 1 h), `zfs-slow-io:<device>` (10 or more in 1 h) (Low): the part wears out.

1. For `ecc-uncorrected`, drain the host (**Dangerous Actions → Drain Server** on the Metal Server). Open a hardware ticket with the provider for the memory module.
2. For a disk, run `smartctl -a /dev/<device>` and plan a replacement before it fails.
3. For heat or throttling, check the fans and temperatures in the BMC, then ask the provider to check the cooling.
4. For corrected errors, open a provider ticket if the count keeps growing.
