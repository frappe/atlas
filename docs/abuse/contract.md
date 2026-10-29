# Abuse protection contract

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Every Atlas change is an optional field, an optional event or a new endpoint. There is no new API version. Old clients, hosts and Central keep working.

Central calls Atlas only from `central/central/integrations/atlas.py`. The Central side of this contract is in `central/spec/WEBHOOKS.md` and `central/spec/INTEGRATIONS.md`.

## Metal and Atlas

| Direction | Channel | Content |
| --- | --- | --- |
| Metal to Atlas | `POST /v1/sync` response, every 10 s | Optional `abuse_signals`. Signals that Atlas did not receive wait in a queue on disk. |
| Metal to Atlas | `POST /v1/sync` response, every 10 s | Optional `restriction_status` list: restriction name with `host_applied_at` when Metal renders it, or an error when Metal cannot render it |
| Metal to telemetry | System telemetry | `metal_abuse_*` metrics. See [telemetry](#telemetry). |
| Atlas to Metal | Host config | Thresholds and their modes, port classes, profile multipliers, breaker durations, package sources, mining pool set |
| Atlas to Metal | Host config | Log bucket endpoint, bucket name and the host write key |
| Atlas to Metal | VM config | `abuse_profile`, overrides, `is_port_25_allowed`, restrictions with `expires_at` |
| Atlas calls Metal | New endpoint | NAT log and SNI log rows of the current hour, which Metal has not uploaded yet |

The sync rules in [host sync](../region/host-sync.md) and the [Atlas to Metal API rules](../interfaces/metal-contract.md#host-sync) apply. An old Atlas ignores `abuse_signals` and `restriction_status`. An old Metal sends neither.

## Atlas API

New routes. All are `central_only`: only a Central token for all tenants can call them.

| Endpoint | Use |
| --- | --- |
| `PUT /api/atlas/virtual-machines/<id>/restrictions` | Apply a restriction. Idempotent by `idempotency_key`. See [apply a restriction](#apply-a-restriction). |
| `DELETE /api/atlas/virtual-machines/<id>/restrictions/<name>` | Lift the restriction with this `name`. Idempotent. |
| `PUT /api/atlas/virtual-machines/<id>/abuse-hold` | Set or clear `has_open_abuse_case`, and an optional legal hold that keeps the evidence snapshot. Idempotent. |
| `GET /api/atlas/virtual-machines/<id>/abuse-signals` | Signals for the evidence pack, Low signals included |
| `POST /api/atlas/abuse/attributions` | Map a list of address, port and time items to a VM ID and a method: Public IP History, NAT Log or Proxy Route. An item with no match returns `unknown`. |

Changed routes, with optional fields only:

| Route | New fields |
| --- | --- |
| VM create | `abuse_profile`, `restrictions` (inherited, see [new servers](enforcement.md#new-servers)) |
| VM update, `central_only` | `abuse_profile`, `abuse_overrides`, `is_port_25_allowed` |
| `GET` VM and `PATCH .../network` responses | `restrictions`: kind, reason code and expiry |

Only Central can set these fields, because the tenant API must not change restrictions. Regenerate the [API clients](../interfaces/api-clients.md) after the change.

### Apply a restriction

Request fields:

| Field | Value |
| --- | --- |
| `idempotency_key` | The name of the Central Resource Action |
| `case_reference` | The Central case, for example `AC-00192` |
| `kind` | `egress_throttle`, `port_block`, `quarantine` or `isolate` |
| `port_classes` | For `port_block` only. Names from Abuse Settings: `auth`, `smtp`, `submission`, `amplification`, `mining`. |
| `ports` | For `port_block` only. A list of ports. Use it in place of `port_classes`. |

- A `port_block` request has `port_classes` or `ports`. Atlas resolves the classes to ports.
- Atlas returns the restriction `name`. Central uses it to lift the restriction.
- A repeat call with the same `idempotency_key` returns the same restriction. Atlas makes no second restriction.

Example:

```json
{
	"idempotency_key": "8f3k2m1q9a",
	"case_reference": "AC-00192",
	"kind": "port_block",
	"port_classes": ["smtp", "submission"]
}
```

### Attribution

Example request:

```json
{
	"items": [
		{"address": "203.0.113.10", "port": 443, "time": 1792483200}
	]
}
```

## Events to Central

Atlas sends the events through the signed Frappe Webhook path in `atlas/vm/core/state_webhook.py`, the same path as [VM state updates](../interfaces/vm-state-updates.md). Central receives them at `central.api.state_delivery.receive`.

| Event | Fields | Sent when |
| --- | --- | --- |
| `vm.abuse.signal` | `signal_id`, `virtual_machine`, `finding_code`, `severity`, `observed_at`, `window_start`, `window_end`, `count`, `evidence`, `circuit_breaker` | The severity is at or above `minimum_severity_sent` (Medium), or a breaker fired, or the code is in `always_sent_finding_codes`. Never for a shadow signal. |
| `vm.restriction` | `restriction_id`, `virtual_machine`, `kind`, `status`, `expires_at`, `applied_by`, `applied_by_user`, `observed_at` | Every change of a VM Restriction |

New fields:

| Field | Event | Value |
| --- | --- | --- |
| `signal_id` | `vm.abuse.signal` | The name of the Abuse Signal |
| `window_start`, `window_end` | `vm.abuse.signal` | The window of the signal |
| `restriction_id` | `vm.restriction` | The name of the VM Restriction |
| `status` | `vm.restriction` | `Pending`, `Applied`, `Lifted` or `Failed` |
| `kind` | `vm.restriction` | `egress_throttle`, `port_block`, `quarantine` or `isolate` |
| `applied_by_user` | `vm.restriction` | The Atlas user who applied an emergency restriction. Empty for all other restrictions. |

- All fields are optional. A consumer must accept an event without them.
- Repeats of one finding in one window merge into one signal with `count`.
- Atlas also sends signals of tenant-0 VMs. Central posts them to Raven `#abuse` and opens no case.
- Central ignores unknown events with HTTP 200, so Central deploys first.
- Atlas keeps signals that it does not send. Central reads them through the signal route.

## HTTP proxy

| Change | Detail |
| --- | --- |
| Rate limit zones | `limit_req` and `limit_conn` for each client address and site. Central can lower them for one site during an attack. Starting values are in [thresholds](detection.md#thresholds). |
| Abuse tombstone | A new route value. Central calls `proxy.suspend_site(name, reason_code)`. See [site tombstone](enforcement.md#site-tombstone). |
| Access log upload | Each node uploads its access log to the log bucket every hour. Atlas writes the endpoint, the bucket name and the proxy write key into the proxy node config. Today the log stays on the node for hours only. |

## IPv6 router

A meter for each source, and a restriction set, so restrictions also apply to routed IPv6. See [inbound DDoS](enforcement.md#inbound-ddos).

## Log upload to Cargo

Cargo owns a tenant-0 log bucket in each region. Cargo owns the object layout, the prefixes, the lifecycle rules and the keys. See [Cargo abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md).

- Each Metal host and proxy node compresses each closed hour with zstd and uploads it as one object. The object key is in the [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout).
- Metal hosts use the host write key. Proxy nodes use the proxy write key. Atlas uses the read key. The key table is in [Cargo keys ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#keys).
- The writer deletes the local file only after a confirmed upload.
- If uploads fail, the host keeps up to 7 days of files and alerts.
- Logs never go to Datum.

## Telemetry

Host safety opens no case and sends no event to Central. Metal sends these metrics in its system telemetry:

| Metric | Value |
| --- | --- |
| `metal_abuse_host_safety_triggers_total{rule}` | Host safety triggers, by rule |
| `metal_abuse_policers_active` | Active host safety policers |
| `metal_abuse_breakers_active{finding_code}` | Active circuit breakers |
| `metal_abuse_breakers_total{finding_code}` | Circuit breakers applied |

An Insights alert on these metrics posts to Raven `#abuse`. See [metrics and alerts](data.md#metrics-and-alerts).

## Deploy order

1. Central: accepts the new events. It calls the new Atlas routes only after step 2.
2. Atlas app: accepts `abuse_signals`, pushes thresholds to hosts and serves the new routes.
3. Cargo: the log bucket and its keys. An operator copies the keys into Abuse Settings.
4. Metal. Uploads start when Atlas writes the host write key into the host config.
5. HTTP proxy: the tombstone, before Central calls `proxy.suspend_site`.

Every threshold stays in Shadow mode until [calibration](detection.md#calibration) confirms it.
