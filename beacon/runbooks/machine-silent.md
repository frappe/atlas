# Machine silent

A host, an Atlas service VM or a Cargo machine that runs, and that sent telemetry in the last 14 days, sent no machine metrics for 3 minutes. The machine can be down, or only its sender stopped: metald on a host, the Datum agent (Vector) on a VM. A tenant VM is not checked. Pilot silent covers a Pilot VM.

1. If many machines of the region are silent at once, check Datum first: Datum ingest stopped, or Region Unreachable.
2. Check whether Host down, VMs unavailable on host or Service node not ready also fires for the machine. Then the machine itself is down: follow that runbook.
3. Open a shell on the machine and check its sender:
   - Host: `systemctl status metal` and `journalctl -u metal --since "15 minutes ago"`.
   - Atlas service VM or Cargo machine: `systemctl status vector` and `journalctl -u vector --since "15 minutes ago"`. A `401` means that the token expired: run Configure Telemetry on the service node, or `datum-refresh` on a Cargo machine.
4. Restart the sender when it stopped.
