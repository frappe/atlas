# Host storage failed

A ZFS pool is not healthy or has data errors, a RAID array is degraded, or a file system is read-only. The host can lose data or stop writes. The alert key names the pool, array or mount.

1. Set **In Maintenance** on the Metal Server in Atlas Desk, so placement puts no new VM there.
2. On the host, run `zpool status -x` for a ZFS alert, `cat /proc/mdstat` for a RAID alert, or `findmnt -o TARGET,OPTIONS` and `dmesg | tail -50` for a read-only file system.
3. Replace or reseat a failed disk through the provider. Then run `zpool replace` or `mdadm --add`, and wait for the resilver.
4. If the pool has data errors, run `zpool status -v` and restore the named files of the affected VMs from backup.
5. Drain the host (**Dangerous Actions → Drain Server**) when the pool or array cannot be repaired soon. See [Diagnose a host](../../docs/operate/diagnose-a-host.md).
