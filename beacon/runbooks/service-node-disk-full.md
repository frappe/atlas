# Service node disk almost full

A proxy, WireGuard gateway or IPv6 router node has less than 10% free (High) or 25% free (Low) on a mount. The alert key is the mount. A full disk stops logs and can stop the service.

1. Open a shell on the node VM. Run `df -h <mount>` and `du -xh --max-depth=2 <mount> | sort -h | tail`.
2. Remove old logs (`journalctl --vacuum-size=500M`) and rotated openresty logs.
3. If logs grow too fast, fix the log rotation of the service.
4. If another node of the service is healthy, traffic fails over. Resize the VM disk when it is too small.
