# datum: metric export

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md)

## Purpose

The `datum` package pushes host and per-VM metrics to the [datum](https://github.com/frappe/datum) telemetry service. It is opt-in: metald runs with it disabled unless `datum.url` is configured.

## Types

| Type | Responsibility |
|---|---|
| `Client` | Posts one sample batch to datum's `/v1/ingest`, bearing one resource's token. |
| `TokenBundle` | The host and per-VM write tokens Atlas shipped to this host. |
| `Exporter` | One export pass: reads the token bundle, gathers host and VM metrics, pushes each under its own token. |
| `VirtualMachineSource`, `HostCapacitySource` | Injected sources: `vm.Manager` and `host.Service` satisfy these directly. |

## Token bundle

Atlas mints the tokens and ships the bundle to this host over SSH, the same way it ships TLS credentials. `Exporter` re-reads the bundle file on every pass, so a re-shipped bundle (rotation, or a newly created VM) takes effect on the next tick without a reload signal.

A VM the bundle carries no token for yet is skipped for that pass, not treated as an error. It is picked up once Atlas re-ships after the VM appears.

## Export

Each resource (the host, each VM) gets its own push, under its own token, with its own short timeout. One resource's failure does not stop the others: `Exporter` logs and continues, because a metrics gap must never affect VM lifecycle operations.

Identity travels only on the bearer token. No sample carries a VM ID or host ID label. That is what the token's `resource_id` claim is for.

## Scheduling

`reconciler.DatumReconciler` wraps `Exporter.PushAll` in the package's shared `passScheduler`, exactly like `ImageReconciler` and `VirtualMachineReconciler`. It ticks on an interval and wakes early alongside the other reconcilers on VM churn.
