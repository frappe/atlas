# Host pool almost full

A ZFS pool of the host is more than 90% used (High) or 80% used (Low). ZFS slows down when a pool is full, and VM disks stop writing at 100%.

1. Set **In Maintenance** on the Metal Server in Atlas Desk, so placement puts no new VM there.
2. On the host, run `zfs list -o name,used,refer -s used` to find the largest datasets and snapshots.
3. Remove old snapshots and unused images.
4. Drain some VMs to a host with free space (**Dangerous Actions → Drain Server**, with **Move Only Selected VMs**) when the pool stays above 90%.
