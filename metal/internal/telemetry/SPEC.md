# telemetry: metrics push to Datum

[Metal specification](../../SPEC.md) · behavior: [Telemetry](../../../docs/operate/telemetry.md)

## Purpose

This package owns metric collection and delivery. Services own their state and report through collectors or `Count`, `Set`, and `Observe`.

## Types

| Type | Responsibility |
|---|---|
| `Registry` | Counters, last-value gauges, bounded observations, and collectors. |
| `Pusher` | Periodic snapshots and retries. |
| `Client` | Batched delivery to Datum. Rereads `datum_url` and `datum_token` from `metald.toml` every 24 hours, or when the token expires. |
| `ProcessCollector` | metald process and build information. |
| `system` package | Host collectors, grouped by subsystem. |

## Invariants

- A label never names a VM, request, migration, or process. The registry rejects these labels and logs the metric once.
- An observation is one sample for one event. The registry keeps at most 50,000 and counts the dropped ones.
- A failed collector does not stop the others. Each collector gets a context with a 10-second deadline and must honor it. The registry logs its first failure and its recovery.
- A push never blocks a VM operation. Callers only take the registry mutex.

## Add a metric

- Count or observe in the package that owns the event. Use `metal_<area>_<name>` and a base unit suffix, such as `_seconds` or `_bytes`.
- Report current state with a `CollectTelemetry` method on the owner, and register it in `cmd/metald/telemetry.go`.
- Do not observe an event that every pass repeats for every VM. Count it instead.
- A kernel table with many fields is one metric with a `field` label, named as the kernel names it.

## Tests

`registry_test.go`, `client_test.go`, and `system/system_test.go` cover label rules, batching, kept observations, and the collector parsers.
