# metald unhealthy

metald on the host does not apply VM changes. The alert key tells why:

- `stalled:<loop>`: a reconcile loop did not run for 5 minutes, or 2 hours for the image loop.
- `pending-change`: a VM change waits for more than 10 minutes.
- `failing-vms`: VMs have an error from metald.
- `dropped-telemetry` (Low): metald dropped telemetry or a collector failed.

1. On the host, run `systemctl status metal.service` and `journalctl -u metal.service -n 300`.
2. Look for the VM or step that repeats an error. A stuck VM operation often blocks the loop.
3. If metald hangs, restart it with `systemctl restart metal.service`. Running VMs keep running.
4. If the error repeats after the restart, read [Diagnose a host](../../docs/operate/diagnose-a-host.md) and escalate to the Atlas team.
