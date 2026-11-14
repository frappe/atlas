# VMs unavailable on host

Three or more VMs, and 20% or more of the VMs that should run on a host, are not available, or every VM that should run is unavailable (High). Fewer unavailable VMs are Medium.

1. Open the host in Atlas Desk and list its VMs with their state.
2. On the host, read `journalctl -u metal.service -n 200` for VM start failures.
3. Check memory and disk of the host. A full disk stops VMs.
4. Post an update on the incident when customer sites are affected.
