# Abuse protection contract

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Every Atlas change is an optional field or a new route. There is no new API version. Old clients, hosts and Central keep working.

Central calls Atlas only from `central/central/integrations/atlas.py`. The Central side of this contract is in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md), `central/spec/WEBHOOKS.md` and `central/spec/INTEGRATIONS.md`.

## Metal and Atlas

Atlas and Metal exchange restrictions only in the VM network object. Atlas sends no restriction data in [host sync](../region/host-sync.md), and Metal reports none there.

| Direction | Channel | Content |
| --- | --- | --- |
| Atlas to Metal | VM create, `PUT /v1/vms/{id}` | The network object with optional `restrictions` and `is_port_25_blocked` |
| Atlas to Metal | `PUT /v1/vms/{id}/network` | The complete network object with the same fields. See [updates replace, not merge](../interfaces/metal-contract.md#updates-replace-not-merge). |
| Metal to Atlas | The VM response | `desired.network` with the same fields, `desired.generation`, `observed.generation` and `observed.error` |
| Atlas to Metal | `metald.toml` | Log bucket endpoint, bucket name, the host write key, and `[telemetry] abuse_metrics` |

New fields of the network object:

| Field | Value |
| --- | --- |
| `restrictions` | Optional list of entries, next to `firewall`. Each entry has `id`, `kind`, `parameters`, optional `expires_at` and `case_reference`. See [restriction kinds](enforcement.md#restriction-kinds). |
| `is_port_25_blocked` | Optional boolean. A missing field means open. See [metal_abuse chain](enforcement.md#metal_abuse-chain). |

- Metal validates the entries before it saves the spec. It rejects an unknown `kind`, and a `port_block` with no `ports`.
- Metal ignores unknown request fields (`decodeJSONRequest` in [request_json.go](../../metal/internal/api/request_json.go)). So an old Metal saves the network without the new fields, and its reply has no `restrictions` key. Atlas reads this as an old Metal. See [restriction API](#restriction-api).
- A new Metal always returns `restrictions`, as `[]` when the VM has none, and `is_port_25_blocked`, as `false` when it is not set.
- On VM create, Atlas does not fail the create when the reply has no `restrictions` key, because the VM exists. The create response then has no `restrictions` and no `is_port_25_blocked`. Central sees this and acts. See [Central restriction states ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#restriction-states).
- Metal reads its saved VM records with `DisallowUnknownFields` ([records.go](../../metal/internal/vm/records.go)). A host that saved a restriction cannot go back to an old metald. A migration target must also run the new metald. Atlas placement does not check the metald version, so every host gets the new Metal before Central sends restrictions.

## Restriction API

New routes. All are `central_only`: only a Central token for all tenants can call them.

| Endpoint | Use |
| --- | --- |
| `PUT /api/atlas/virtual-machines/<id>/restrictions` | Apply a restriction. Idempotent by `idempotency_key`. |
| `PATCH /api/atlas/virtual-machines/<id>/restrictions/<restriction_id>` | Change `expires_at` of an entry, to extend or shorten it. The body holds only `expires_at`. |
| `DELETE /api/atlas/virtual-machines/<id>/restrictions/<restriction_id>` | Lift the entry with this ID. Lifting a missing entry succeeds. |

`restriction_id` is the `id` of the entry: the `idempotency_key` of the apply call, or the `atlas-` ID of an [operator entry](enforcement.md#operator-emergency-restriction).

Each route takes the per-VM network lock, reads the network from Metal, merges one change and sends the complete network back. See [restrictions in the network object](enforcement.md#restrictions-in-the-network-object).

Apply request:

| Field | Value |
| --- | --- |
| `idempotency_key` | The name of the Central Resource Action. Atlas uses it as the entry `id`. |
| `case_reference` | The Central case, for example `AC-00192` |
| `kind` | `egress_throttle`, `ingress_throttle`, `port_block`, `quarantine` or `isolate` |
| `parameters` | `ports` for `port_block`: a list of `{port, protocol}` objects. `pps` and `bps` for a throttle. Empty for `quarantine` and `isolate`. See [restriction kinds](enforcement.md#restriction-kinds). |
| `expires_at` | Optional. Metal skips the entry after this time. |

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

The response of each route:

| Field | Value |
| --- | --- |
| `restrictions` | The `restrictions` list of the VM, from the desired network in Metal's reply |
| `is_port_25_blocked` | From the desired network. `false` when the field is missing. |
| `desired_generation` | `desired.generation` from Metal |
| `observed_generation` | `observed.generation` from Metal |
| `host_error` | The message of `observed.error` from Metal, or `null` |

```json
{
	"restrictions": [
		{
			"id": "8f3k2m1q9a",
			"kind": "port_block",
			"parameters": {"ports": [{"port": 465, "protocol": "tcp"}, {"port": 587, "protocol": "tcp"}]},
			"expires_at": "2026-11-12T10:00:00Z",
			"case_reference": "AC-00192"
		}
	],
	"is_port_25_blocked": true,
	"desired_generation": 42,
	"observed_generation": 41,
	"host_error": null
}
```

Rules:

- If an entry with the `idempotency_key` exists, Atlas returns it and changes nothing.
- A call with a new `idempotency_key` adds a separate entry, also when the VM has one of the same kind. When two active entries of one kind overlap, Metal renders the stricter one.
- A `port_block` request with no `ports` gets a validation error. Atlas does not accept port classes.
- `PATCH` changes only `expires_at`. Lifting with `DELETE` an entry that is not there returns the current list.
- If the network in Metal's reply has no `restrictions` key, the host runs an old Metal. Atlas returns `409 host_unsupported`. The old Metal keeps no restriction. Central sets its row to `Failed`.
- A VM of tenant 0 gets `403`.
- When another network change holds the lock for more than 10 s, Atlas returns the existing `409` conflict error. Its `error.code` is not `host_unsupported`. Central retries with the same `idempotency_key`.
- Errors use the Atlas error format: `{"error": {code, message, fields}}`.

Changed routes, with optional fields only:

| Route | New fields |
| --- | --- |
| VM create | `restrictions`: inherited entries, in the apply format, each with its own `idempotency_key` and `case_reference`. `is_port_25_blocked`. Both are top-level fields of the create payload, like `firewall`. Atlas puts them in the Metal network object. |
| `GET` VM | `restrictions`, `is_port_25_blocked`, `desired_generation`, `observed_generation` and `host_error`. Atlas reads them from Metal, like routes. The Central repair scan uses them. |

Atlas accepts the two create fields only from a Central identity (`require_central_identity()` in [identity.py](../../atlas/auth/identity.py)). Any other caller that sends them gets `403`. The create route stays open to tenants.

Where the code goes. Follow [add a route](../develop/atlas-app.md#add-a-route):

- A resource group in [router.py](../../atlas/api/router.py), and the routes in a new file `atlas/api/routes/virtual_machine_restrictions.py`. Import it in `register_atlas_api`. The existing files have plural resource names, for example `virtual_machines.py` and `public_ips.py`.
- Pydantic request and response models in [models.py](../../atlas/api/models.py). The router rejects union types, so `parameters` is one `StrictModel` with optional `ports`, `pps` and `bps`. Atlas checks them against `kind`.
- The domain logic in `VirtualMachineService`, not in the route.
- Regenerate the [API clients](../interfaces/api-clients.md) after the change.

## Port 25 API

| Endpoint | Use |
| --- | --- |
| `PUT /api/atlas/virtual-machines/<id>/port-25` | Set `is_port_25_blocked` in the VM network object. `central_only`. The body is `{"is_blocked": true}` or `{"is_blocked": false}`. |

- Atlas uses the same lock and the same read, merge and replace path as the [restriction API](#restriction-api).
- The response is the same as the response of the restriction API.
- The old Metal check, the tenant 0 guard and the lock timeout are the same.
- Central clears the block when it accepts a Port 25 Request, and sets it again when it revokes the request.

## Telemetry

Metal exports the `vm_abuse_*` metrics for each VM to Datum at the metald telemetry interval. The metric names and labels are in [counters](metrics.md#counters).

- Metal exports them only when `metald.toml` has `[telemetry] abuse_metrics = true`. The default is false.
- Atlas writes `abuse_metrics` from the Atlas Settings check `is_abuse_metrics_enabled`, with the other `[telemetry]` values (`HostInstallation.install_telemetry` and [install-telemetry.sh](../../atlas/scripts/install-telemetry.sh)). Layout: [Atlas Settings](layouts.md#atlas-settings-layout-change).
- An operator turns on `is_abuse_metrics_enabled` only after the Datum release: the TTL table, the `vm_abuse_*` routing and the `watcher` user.

Cargo keeps the series in a separate Datum table. Watcher reads them with the read-only user `watcher`. See [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics).

## Log upload

Cargo owns a tenant-0 log bucket in each region. Cargo owns the object layout and the lifecycle rules.

| Log | Writer | Key |
| --- | --- | --- |
| `nat`, see [NAT log](metrics.md#nat-log) | Metal host | The write key of the host |
| `sni`, see [SNI log](metrics.md#sni-log) | Metal host | The write key of the host |
| `proxy-access` | HTTP proxy node | The write key of the proxy node |

- Each writer compresses its log with zstd and uploads one object every 5 min. The object key is in the [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout).
- Each writer has its own write key. See [log bucket keys](#log-bucket-keys).
- Atlas has no read key and does not read the logs.
- The writer deletes the local file only after a confirmed upload.
- If uploads fail, the writer keeps up to 7 days of files and logs each failure at error level.
- Logs never go to Datum.

Today a proxy node keeps its access log for hours only.

## Log bucket keys

Atlas gets one write key for each Metal Server and for each Proxy Server from Cargo. No operator copies a key.

- Atlas calls Cargo with an Atlas-issued tenant-0 token, as `CargoBucket` does ([bucket.py](../../atlas/service/core/cargo/bucket.py)): the `X-Cargo-Access-Token` header, 5 min lifetime.
- Cargo has `add_credentials`, `rotate_credentials` and `remove_credentials` in `cargo/object_storage/api/bucket.py`.
- Cargo needs one change: `add_credentials` takes an optional `permission`, `write` or `read`. The default is the current behavior. Atlas always asks for `write`, so a host key cannot read the logs. A Garage write key can also delete objects. Watcher finds a deleted object. See [Watcher log integrity ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#log-integrity).

Life of a key:

| Step | Metal Server | Proxy Server |
| --- | --- | --- |
| Create | When the host is set up | When the node is set up |
| Write | Into `metald.toml` on the host | Into the proxy config, `/etc/atlas/proxy-control.toml` (`ProxyConfiguration`) |
| Rotate | Every 30 days | Every 30 days |
| Remove | When the host is removed | When the node is removed |

- Atlas stores the key ID and the secret on the Metal Server or the Proxy Server. The secret is a Password field. Layout: [Metal Server and Proxy Server](layouts.md#metal-server-and-proxy-server-layout-change).
- To rotate a key, Atlas calls `add_credentials`, writes the new key to the host or node, and then calls `remove_credentials` for the old key. Thus uploads do not stop. `rotate_credentials` ends the old key at once, before the new key is on the writer.
- A key for each writer lets an operator revoke one host. Garage does not record which key wrote an object.
- The bucket name and endpoint are in the "Log Bucket" section of Atlas Settings. An operator enters them once for each region, from the Cargo setting. They are not secret. Layout: [Atlas Settings](layouts.md#atlas-settings-layout-change).
- The Watcher read key stays in Cargo. See [Cargo keys ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#keys).

## Time source

Log times must be traceable for CERT-In. No NIC or NPL time server is set today (`metal/cmd/metald/config.go`). Point chrony on hosts and service VMs at `samay1.nic.in`, `samay2.nic.in` and `time.nplindia.org`. Check the hostnames first. See the [Central CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md).

## Deploy order

The deploy order of all components is in [Central deploy order ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#deploy-order). For Atlas:

1. The Atlas app: the restriction and port 25 routes, the create fields, key provisioning and the Atlas Settings fields. A Frappe patch makes a write key for each existing Metal Server and Proxy Server, and Atlas writes it into `metald.toml` and `proxy-control.toml` in the same way as at setup.
2. Metal: `restrictions` and `is_port_25_blocked` in the network object, the `metal_abuse` chain, the counters behind `abuse_metrics`, and log upload.
3. The HTTP proxy: the tombstone and log upload.
4. After the Datum release, an operator turns on `is_abuse_metrics_enabled`.
