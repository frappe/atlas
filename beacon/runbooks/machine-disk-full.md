# Machine disk almost full

A file system of a host or a VM that is not ZFS has less than 10% free (High) or 25% free (Low). The alert key is the mount. A full root or log disk stops the service of the machine and its telemetry.

1. Open a shell on the machine. Run `df -h <mount>` and `du -xh --max-depth=2 <mount> | sort -h | tail` to find what grew.
2. Remove what is safe to remove: old journal files (`journalctl --vacuum-size=500M`), rotated logs, old images and core dumps.
3. If a service writes too much, fix its log level or rotation.
4. If the disk is too small for its job, plan a larger disk. On a service VM, another node of the service takes the traffic while you resize.
