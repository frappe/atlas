# Abuse protection contract

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Every Atlas change is an optional field, an optional event or a new endpoint. There is no new API version. Old clients, hosts and Central keep working.

Central calls Atlas only from `central/central/integrations/atlas.py`. The Central side of this contract is in `central/spec/WEBHOOKS.md` and `central/spec/INTEGRATIONS.md`.

## Metal and Atlas

| Direction | Channel | Content |
| --- | --- | --- |
| Metal to Atlas | `POST /v1/sync` response, every 10 s | Optional `restriction_status` list: restriction name with `host_applied_at` when Metal renders it, or an error when Metal cannot render it |
| Atlas to Metal | Host config | Log bucket endpoint, bucket name and the host write key |
| Atlas to Metal | VM config | `is_port_25_allowed`, and the restrictions with name, kind, parameters and `expires_at` |

The sync rules in [host sync](../region/host-sync.md) and the [Atlas to Metal API rules](../interfaces/metal-contract.md#host-sync) apply. An old Atlas ignores `restriction_status`. An old Metal does not send it.

## Restriction API

New routes. All are `central_only`: only a Central token for all tenants can call them.

| Endpoint | Use |
| --- | --- |
| `PUT /api/atlas/virtual-machines/<id>/restrictions` | Apply a restriction. Idempotent by `idempotency_key`. |
| `PATCH /api/atlas/virtual-machines/<id>/restrictions/<name>` | Change `expires_at` of an active restriction, to extend or shorten it. The body holds only `expires_at`. Idempotent. |
| `DELETE /api/atlas/virtual-machines/<id>/restrictions/<name>` | Lift the restriction with this `name`. Idempotent. |

Changed routes, with optional fields only:

| Route | New fields |
| --- | --- |
| VM create | `restrictions`: inherited restrictions, in the apply format. Each has its own `idempotency_key` and `case_reference`. |
| VM update, `central_only` | `is_port_25_allowed` |
| `GET` VM and `PATCH .../network` responses | `restrictions`: name, kind, status, case reference and expiry |

Only Central can set these fields, because the tenant API must not change restrictions. Regenerate the [API clients](../interfaces/api-clients.md) after the change.

Apply request:

| Field | Value |
| --- | --- |
| `idempotency_key` | The name of the Central Resource Action |
| `case_reference` | The Central case, for example `AC-00192` |
| `kind` | `egress_throttle`, `ingress_throttle`, `port_block`, `quarantine` or `isolate` |
| `parameters` | `ports` for `port_block`: a list of port and protocol. `pps` and `bps` for a throttle. Empty for `quarantine` and `isolate`. See [restriction kinds](enforcement.md#restriction-kinds). |
| `expires_at` | Optional. Metal and Atlas end the restriction at this time. |

- Atlas returns the restriction `name`. Central uses it to lift the restriction.
- A repeat call with the same `idempotency_key` returns the same restriction. Atlas makes no second restriction.
- A call with a new `idempotency_key` makes a separate restriction, also when the VM has one of the same kind.
- When two active restrictions of one kind overlap, Metal renders the stricter one.
- A `port_block` request with no `ports` gets a validation error. Atlas does not accept port classes.
- Errors use the Atlas error format: `{"error": {code, message, fields}}`.

Example:

```json
{
	"idempotency_key": "8f3k2m1q9a",
	"case_reference": "AC-00192",
	"kind": "port_block",
	"parameters": {
		"ports": [
			{"port": 465, "protocol": "tcp"},
			{"port": 587, "protocol": "tcp"}
		]
	},
	"expires_at": "2026-11-12T10:00:00Z"
}
```

## Events to Central

Atlas sends the event through the signed Frappe Webhook path in `atlas/vm/core/state_webhook.py`, the same path as [VM state updates](../interfaces/vm-state-updates.md). Central receives it at `central.api.state_delivery.receive`.

| Event | Fields | Sent when |
| --- | --- | --- |
| `vm.restriction` | `restriction_id`, `virtual_machine`, `kind`, `status`, `idempotency_key`, `case_reference`, `expires_at`, `applied_by_user`, `lifted_by` | Every status change of a VM Restriction |

| Field | Value |
| --- | --- |
| `restriction_id` | The name of the VM Restriction |
| `kind` | A [restriction kind](enforcement.md#restriction-kinds) |
| `status` | `Pending`, `Applied`, `Lifted` or `Failed` |
| `idempotency_key` | Optional. The `idempotency_key` of the apply request. Empty for an emergency restriction. |
| `case_reference` | Optional. The Central case of the apply request. |
| `expires_at` | Optional. The current expiry of the restriction. |
| `applied_by_user` | Optional. The Atlas operator who applied an emergency restriction. |
| `lifted_by` | Optional. Only when `status` is `Lifted`: `Central`, `Expiry` or `Atlas user`. |

- All fields are optional. A consumer must accept an event without them.
- Central ignores unknown events with HTTP 200.

## Telemetry

Metal exports the `vm_abuse_*` metrics for each VM to Datum at the metald telemetry interval. The metric names and labels are in [counters](metrics.md#counters).

Cargo keeps the series in a separate Datum table. Central reads them with a read-only user. See [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics).

## Log upload

Cargo owns a tenant-0 log bucket in each region. Cargo owns the object layout, the lifecycle rules and the keys.

| Log | Writer | Key |
| --- | --- | --- |
| `nat`, see [NAT log](metrics.md#nat-log) | Metal host | Host write key |
| `sni`, see [SNI log](metrics.md#sni-log) | Metal host | Host write key |
| `proxy-access` | HTTP proxy node | Proxy write key |

- Each writer compresses its log with zstd and uploads one object every 5 min. The object key is in the [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout).
- An operator copies the write keys from Cargo into Abuse Settings. Atlas writes the host write key into the metald host config, and the proxy write key into the proxy node config. See [Cargo keys ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#keys).
- Atlas has no read key and does not read the logs.
- The writer deletes the local file only after a confirmed upload.
- If uploads fail, the writer keeps up to 7 days of files and logs each failure at error level.
- Logs never go to Datum.

Today a proxy node keeps its access log for hours only.

## Deploy order

The deploy order of all components is in [Central deploy order ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#deploy-order).
