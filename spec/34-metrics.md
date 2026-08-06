# Metrics — host and per-VM telemetry shipped to datum

The README's oldest non-goal — "No metrics or alerting. `journalctl` is enough."
([README](./README.md#non-goals-this-iteration)) — was scoped exactly as far as
[22-observability.md](./22-observability.md) drew it: observability stops at
making long-running *tasks* legible to a human, and spec/22 explicitly disclaimed
metrics, alerting, and time-series. This chapter retires the **metrics half** of
that disclaimer. It ships what spec/22 declined to specify: machine telemetry —
host and per-VM time-series — pushed to an external store, so the fleet's
questions ("is this host low on RAM? which tenants are burning CPU? is a VM's
disk filling?") are answered by numbers a chart can hold, not by grepping a
journal.

It does not supersede spec/22; the two are different layers and both stay. The
Task row ([22](./22-observability.md)) answers "what is the thing I clicked doing
right now" — live, human, ephemeral. The metric answers "what have the machines
done, over time, across the fleet" — historical, aggregate, queryable. One is
liveness for an operator watching; the other is a time-series for a fleet an
operator is not watching. Nothing in spec/22 is retracted; this chapter adds the
layer it explicitly left out.

## The store: frappe/datum

`datum` is a ClickHouse-backed telemetry service. Producers POST JSON samples to
`/v1/ingest` under a bearer JWT; readers query SQL at `/v1/query`. A sample is
one point in one series:

```
{metric, value, ts, labels}   # ts is ISO-8601 Z
```

datum stamps every sample with the `resource_id` carried in the producer's JWT,
and it cannot be overridden per sample. That is the tenant boundary, and it is
deliberately out of the producer's hands: a producer *is* a resource — its token
says which one — so a buggy or malicious sample cannot claim another tenant's
series, no matter what it puts in `labels`. The same property makes read grants
safe in the other direction: a query credential scoped to one `resource_id`
leaks exactly that resource's series and nothing else.

## The producer: boat

The push lives in `boat`, the per-host Go daemon ([33-boat.md](./33-boat.md)),
on a resident ticker defaulting to 30 seconds. The choice is attachment, not
invention: boat already runs on every host, already gathers host facts for its
export, and already runs a 30s reconcile loop
([33-boat.md § 3.2](./33-boat.md#32-reconciler-and-forward-only-state-machines))
— so the metric tick rides machinery that exists instead of a new agent, in the
spirit of [README principle 5](./README.md#operating-principles) (no agent runs
on the server). A separate collector would duplicate boat's host probes, its VM
inventory, and its failure handling for no gain.

The push is strictly best-effort, by construction. Each tick runs in an isolated
background worker with a 2-second HTTP timeout, no retry, and bounded per-VM
concurrency. There is no retry queue, no backpressure into the reconciler, no
shared lock with anything that touches a VM. datum being slow or down is
therefore a gap in a chart — the worst case is a missing data point — never a
slow or failed VM operation. Best-effort is a deliberate trade, not a shortcut:
telemetry is the one output where losing a sample is strictly cheaper than any
mechanism that could guarantee delivery, and a guarantee would put the VM
lifecycle on the critical path of a third-party service Atlas does not control.

The tick is also strictly opt-in. It is wired only when `--datum-url` is set
(from the `atlas_datum_url` site-config key); an un-wired host reads no token
file, starts no tick, opens no connection — it behaves exactly as it did before
this feature existed. Metrics export is invisible until it is configured.

## Tenancy is per-VM

`resource_id` is the Frappe **Server** name for host samples and the **Virtual
Machine** name — which is the VM's UUID — for a VM's samples. Because datum
stamps one `resource_id` per batch and a token names one resource, boat holds
**N+1 tokens** — one host token plus one token per VM — and pushes one batch per
`resource_id` on each tick.

The cost is token churn: a VM's token is minted when the VM appears and dropped
when it leaves, and each tick pays N+1 HTTP round trips instead of one. The cost
is deliberate. A single host-scoped `resource_id` would have made the host the
tenant — cheaper, but it is the wrong boundary: a tenant's VM is exactly the unit
a hosting platform must be able to talk about alone. With per-VM ids, a single
VM's series can be read — or a read grant handed out for it — in isolation,
without exposing the rest of the host's fleet, and the series follows the VM
wherever it runs, because its identity is the resource, not the machine. That is
the property the host-scoped alternative cannot give, and it is the one that
matters.

## Token lifecycle

Atlas mints the tokens ([datum_token.py](../atlas/atlas/datum_token.py)): RS256,
signed with the fleet key in site config `atlas_datum_signing_key`; datum
verifies with the matching public key, and `atlas_datum_key_id` rides the JWT
`kid` header so the fleet key can rotate without touching datum. Each token
carries `resource_id`, `access: ["write"]`, `iat`, and `exp`; the default TTL is
24 hours, and tokens are re-minted on a refresh sweep, so a day comfortably
covers a missed sweep. Minting is pure crypto — the module imports and tests
without a Frappe site.

Atlas ships the batch to the host as `/etc/boat/datum-tokens.json`, one JSON
document:

```json
{"host": "<jwt>", "vms": {"<uuid>": "<jwt>"}}
```

installed `0640 root:boat`, and the secret travels over **stdin, never argv** —
argv is world-readable and would leak a tenant's write credential into the
process list. boat reads the file at startup and **reloads it on SIGHUP**, which
is the whole rotation story: Atlas rotates the fleet key or reflects VM churn by
rewriting the file and `systemctl reload boat`. No restart, no dropped ticks, no
window where a token is half-valid. The three config keys, all of them:
`atlas_datum_url`, `atlas_datum_signing_key`, `atlas_datum_key_id`.

## Collection needs no new privilege

Host gauges come from the same host probes boat already runs for its export —
CPU count, memory, pool fullness are facts it already gathers, now sampled on
the tick instead of only on the export sweep.

Per-VM values are read **in-process from world-readable files**, needing no sudo
and no guest access. Two sources, both on the host:

- The VM's **cgroup v2 tree** at `/sys/fs/cgroup/firecracker/<uuid>/` —
  `memory.current` and `memory.max` for the memory gauges, `cpu.stat`'s
  `usage_usec` for CPU, `io.stat`'s `rbytes`/`wbytes` for IO.
- The host-side **veth** `atlas-h<first-7-hex-of-uuid>` under
  `/sys/class/net/<veth>/statistics/` — `rx_bytes`/`tx_bytes` for network. The
  veth faces the host, so its rx is the guest's tx and its tx is the guest's rx:
  `vm_network_receive_bytes_total` reads the veth's `tx_bytes`,
  `vm_network_transmit_bytes_total` its `rx_bytes`.

A missing file — a VM not running, or torn down between ticks — drops **that one
sample, never the batch**; the tick moves on and the rest of the VMs report. A
non-Running VM reports only `vm_up=0`: the cgroup and veth are gone, and the
status label is the only signal left.

## Counters stay cumulative

CPU, IO, and network metrics are **cumulative counters** — the names end
`_total` — and the rate is derived downstream by datum/Grafana. That division of
labor is why boat keeps **no cross-tick state**: it never stores a previous
value, so there is nothing to persist, nothing to recover after a crash, and
nothing to get wrong when a counter resets. A VM restart zeroes its counters;
the derived rate dips to zero and resumes, which is exactly the shape a restart
deserves, and because the reset is visible downstream rather than "handled" by a
producer that keeps history, the chart stays honest.

## The metric set

Host — `resource_id` is the Server name; no identifying labels:

| Metric | Meaning |
| --- | --- |
| `host_up` | 1 while the host's push succeeds |
| `host_cpu_cores` | host CPU core count |
| `host_memory_bytes` | total host memory |
| `host_disk_free_bytes` | free disk on the host's storage pool |
| `host_pool_data_percent` | thin-pool data fullness — the number placement's capacity accounting reads |
| `host_pool_metadata_percent` | thin-pool metadata fullness |
| `host_virtual_machines` | VMs on the host |
| `host_firecracker_running` | running firecracker processes |

Per-VM — `resource_id` is the VM name; every sample carries the `server` label,
and `vm_up` additionally carries `status`:

| Metric | Meaning |
| --- | --- |
| `vm_up` | 1 while the VM is Running, 0 otherwise (labeled with `status`) |
| `vm_memory_current_bytes` | cgroup `memory.current` |
| `vm_memory_max_bytes` | cgroup `memory.max` |
| `vm_cpu_usage_seconds_total` | counter — cgroup `cpu.stat` `usage_usec` |
| `vm_io_read_bytes_total` | counter — cgroup `io.stat` `rbytes` |
| `vm_io_write_bytes_total` | counter — cgroup `io.stat` `wbytes` |
| `vm_network_receive_bytes_total` | counter — veth `tx_bytes` (the guest's rx) |
| `vm_network_transmit_bytes_total` | counter — veth `rx_bytes` (the guest's tx) |

Names are lowercase `^[a-z_][a-z0-9_]*$`, the identifier grammar datum ingests.
One label key is off the table: **`uuid` is forbidden by datum**, so per-VM
identity rides the `resource_id`, not a label — which is consistent with the
tenancy model. The resource is the tenant, and a VM's series is named by the
resource it belongs to, not by a label any producer could mistype.

## What this is not

- **Not a supersession of spec/22.** Task liveness and fleet numbers are
  different layers; both stay
  ([22-observability.md](./22-observability.md),
  [33-boat.md § 10](./33-boat.md#10-observability-and-audit)).
- **Not alerting.** This chapter ships numbers, not pages. Alerting on the
  series is a consumer's job and stays out of scope — the non-goal's alerting
  half stands.
- **Not a new agent, and not on the VM lifecycle path.** The tick is a
  best-effort reader inside boat: it can lose samples, but it can never hold a
  lock, delay a verb, or fail a VM operation.
- **Not a per-sample tenancy override.** `resource_id` comes from the token;
  samples cannot assert it.

## Testing

The split mirrors the rest of the spec. The token mint is pure crypto with no
Frappe dependency: [test_datum_token.py](../atlas/tests/test_datum_token.py)
verifies the claims (`resource_id`, `access`, `exp`), the `kid` header, and that
a token signed by the wrong key is rejected — milliseconds, no site, no host.

The pipeline is a host fact: a live host pushing into a real datum store,
asserted end-to-end — the tick fires, samples land under the right
`resource_id`, and the failure shapes are the honest ones: datum down is a gap
in the series, not a failed tick; a VM torn down between ticks loses one sample
with the batch intact; a non-Running VM yields `vm_up=0` and nothing else. The
2-second timeout and no-retry behavior make these shapes cheap to provoke and
assert.

## Files

Controller: [datum_token.py](../atlas/atlas/datum_token.py) (mint, refresh,
ship) and [test_datum_token.py](../atlas/tests/test_datum_token.py). Host: the
metric tick inside boat ([33-boat.md](./33-boat.md)).
