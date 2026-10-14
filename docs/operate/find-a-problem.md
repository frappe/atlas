# Find and fix a problem

Start with the first failed step. Find its owning component before you retry. Keep uncertain records until that owner confirms the result. A timeout can leave an accepted request in progress. Keep the operation ID, fix the cause, and let the owner retry.

To go from a dashboard alert to one host, see [Diagnose a host](diagnose-a-host.md). Each component has a runbook with detailed checks: [Atlas](atlas.md), [Metal](metal.md), [Telemetry](telemetry.md), [HTTP proxy recovery](../networking/http-proxy/high-availability.md), and [WG Mesh operations](../networking/wg-mesh/operations.md). Read the [incident reports](../incidents/) before you change recovery behavior.

## Find your symptom

| Symptom | First check | Next guide |
| --- | --- | --- |
| Metal Server stays `Pending` or `Failed` | Read status, provider ID, Atlas Error Log phase, and SSH Task result. | [Host provisioning](../region/index.md) |
| VM stays a draft | Read it from the assigned Metal host. Check the draft reconciliation Error Log. | [VM lifecycle](../compute/index.md) |
| Atlas cannot choose a host | Check sample age, host pool, image architecture, and draft reservations. | [Choose a host](../compute/placement.md) |
| VM does not boot | Compare Metal desired and observed generations, phase, error, systemd unit, and ZFS pool. | [Metal operations](metal.md) |
| VM list state looks old | Check latest Metal Server Usage sample and the host sync Error Log. | [Host sync](../region/host-sync.md) |
| VM has no private network | Check its Metal namespace, peer set, and mesh location. | [Traffic paths](../networking/traffic.md) |
| Site HTTP or HTTPS fails | Check DNS, proxy readiness, route generation, then mesh reachability. | [HTTP proxy](../networking/http-proxy/index.md) |
| Direct or routed public IP fails | Check the allocation request, host route or router VM, and the VM network route. | [Public IPs](../networking/public-ips.md) |
| Image transfer stalls | Check Atlas image status and upload IDs, Metal snapshot status, and object storage access. | [Images and snapshots](../storage/index.md) |
| Migration stalls | Compare Atlas migration status with source and destination Metal records. | [Migration](../compute/migration.md) |

## Find the logs and records

| Component | Logs and state |
| --- | --- |
| Atlas app | Error Log, SSH Tasks, and the latest `Metal Server Usage` timestamp. |
| Metal | VM records under its base directory. JSON logs in the host journal and in Datum. |
| VM host resources | `metal-vm@<id>.service`, `metal-<id>` namespace, and ZFS disk. |
| HTTP proxy | `/var/lib/nginx/cluster-state.json`. |
| WG Mesh | eBPF forwarding maps. |

## Safe recovery rule

1. Read the owner's record and operation ID.
2. Correct the underlying fault.
3. Let the owning reconciler retry.

A lost response can mean the request was accepted. **Keep drafts, host IDs, upload IDs, migration records, saved state, and ZFS datasets** until the owner confirms cleanup is safe.

For detailed host commands, use [Metal operations](metal.md). For regional jobs and records, use [Atlas operations](atlas.md). For packet inspection, use the [WG Mesh operations guide](../networking/wg-mesh/operations.md) and the [proxy setup checks](../networking/http-proxy/install.md).

::: details Source code and tests

- [Atlas scheduler](../../atlas/hooks.py) queues regional reconciliation jobs.
- [Host provisioner](../../atlas/metal_server/core/provisioning.py) records setup failure status.
- [Atlas sync job](../../atlas/metal_server/usage.py) stores host capacity and VM reports.
- [VM reconciliation](../../atlas/vm/core/reconciliation.py) settles uncertain create and delete results.
- [Metal VM reconciliation](../../metal/internal/vm/reconcile.go) stores host phases and errors.

:::
