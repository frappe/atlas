# Service node silent

A proxy, WireGuard gateway or IPv6 router node that Atlas runs sent telemetry before and stopped for 3 minutes. The node can be down, or only its Vector agent stopped. An IPv6 router has no readiness probe, so its silence pages (High). Other nodes are Medium.

1. Check whether Service node not ready also fires for the node. Then the node itself is down: follow that runbook.
2. Open a shell on the node VM. Run `systemctl status vector` and `journalctl -u vector --since "15 minutes ago"`.
3. Restart Vector with `systemctl restart vector` when it stopped.
4. If every node of the region is silent at once, check Datum ingestion first.
