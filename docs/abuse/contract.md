# Abuse protection contract

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Every Atlas change is an optional field or a new route. There is no new API version. Old clients, hosts and Central keep working.

Central calls Atlas only from `central/central/integrations/atlas.py`. The Central side of this contract is in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md), `central/spec/WEBHOOKS.md` and `central/spec/INTEGRATIONS.md`.

## Metal and Atlas

Atlas and Metal exchange restrictions only in the VM network object. Atlas sends no restriction data in [host sync](../region/host-sync.md). Metal reports there only the generations and the error of each VM, for the [`vm.network` event](#events-to-central).

| Direction | Channel | Content |
| --- | --- | --- |
| Atlas to Metal | VM create, `PUT /v1/vms/{id}` | The network object with optional `restrictions` and `is_mail_ports_blocked` |
| Atlas to Metal | `PUT /v1/vms/{id}/network` | The complete network object with the same fields. See [updates replace, not merge](../interfaces/metal-contract.md#updates-replace-not-merge). |
| Metal to Atlas | The VM response | `desired.network` with the same fields, `desired.generation`, `observed.generation` and `observed.error` |
| Metal to Atlas | Host sync, `POST /v1/sync` | For each VM in `virtual_machines`: optional `network_desired_generation`, `network_observed_generation` and `network_error` |
| Atlas to Metal | `metald.toml` | Log bucket endpoint, bucket name, the host write key, `[telemetry] abuse_metrics`, `[abuse] logs`, `[nat] is_port_range_enabled` and `[protect] mode` |

`metald.toml` also has the optional values `[protect] exempt_prefixes` and `[abuse] conntrack_entries_per_vm`. Atlas does not write them. See [rollout](protection.md#rollout).

New fields of the network object:

| Field | Value |
| --- | --- |
| `restrictions` | Optional list of entries, next to `firewall`. Each entry has `id`, `kind`, `parameters`, optional `expires_at` and `case_reference`. See [restriction kinds](enforcement.md#restriction-kinds). |
| `is_mail_ports_blocked` | Optional boolean. A missing field means open. See [metal_abuse chain](enforcement.md#metal_abuse-chain). |

- Metal validates the entries before it saves the spec. It rejects an unknown `kind`, and a `port_block` with no `ports`.
- Metal ignores unknown request fields (`decodeJSONRequest` in [request_json.go](../../metal/internal/api/request_json.go)). So an old Metal saves the network without the new fields, and its reply has no `restrictions` key. Atlas reads this as an old Metal. See [restriction API](#restriction-api).
- A new Metal always returns `restrictions`, as `[]` when the VM has none, and `is_mail_ports_blocked`, as `false` when it is not set.
- On VM create, Atlas does not fail the create when the reply has no `restrictions` key, because the VM exists. The create response then has no `restrictions` and no `is_mail_ports_blocked`. Central sees this and acts. See [Central restriction states ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#restriction-states).
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
| `is_mail_ports_blocked` | From the desired network. `false` when the field is missing. |
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
	"is_mail_ports_blocked": true,
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
| VM create | `restrictions`: inherited entries, in the apply format, each with its own `idempotency_key` and `case_reference`. `is_mail_ports_blocked`. Both are top-level fields of the create payload, like `firewall`. Atlas puts them in the Metal network object. |
| `GET` VM | `restrictions`, `is_mail_ports_blocked`, `desired_generation`, `observed_generation` and `host_error`. Atlas reads them from Metal, like routes. The Central repair scan uses them. |

Atlas accepts the two create fields only from a Central identity (`require_central_identity()` in [identity.py](../../atlas/auth/identity.py)). Any other caller that sends them gets `403`. The create route stays open to tenants.

Where the code goes. Follow [add a route](../develop/atlas-app.md#add-a-route):

- A resource group in [router.py](../../atlas/api/router.py), and the routes in a new file `atlas/api/routes/virtual_machine_restrictions.py`. Import it in `register_atlas_api`. The existing files have plural resource names, for example `virtual_machines.py` and `public_ips.py`.
- Pydantic request and response models in [models.py](../../atlas/api/models.py). The router rejects union types, so `parameters` is one `StrictModel` with optional `ports`, `pps` and `bps`. Atlas checks them against `kind`.
- The domain logic in `VirtualMachineService`, not in the route.
- Regenerate the [API clients](../interfaces/api-clients.md) after the change.

## Mail ports API

| Endpoint | Use |
| --- | --- |
| `PUT /api/atlas/virtual-machines/<id>/mail-ports` | Set `is_mail_ports_blocked` in the VM network object. `central_only`. The body is `{"is_blocked": true}` or `{"is_blocked": false}`. |

- The block covers TCP ports 25, 465 and 587, in both directions, for remote addresses outside the mesh. See [metal_abuse chain](enforcement.md#metal_abuse-chain).
- Atlas uses the same lock and the same read, merge and replace path as the [restriction API](#restriction-api).
- The response is the same as the response of the restriction API.
- The old Metal check, the tenant 0 guard and the lock timeout are the same.
- Central clears the block when it accepts a Mail Port Request, and sets it again when it revokes the request.

## Events to Central

Atlas tells Central when the network of a VM changes on the host. Central then does not wait for its repair scan.

The existing pattern: `DELIVERIES` in [state_webhook.py](../../atlas/vm/core/state_webhook.py) sends `vm.state` and `vm.state.deleted` from the Virtual Machine State DocType, through a Frappe Webhook, to `central.api.state_delivery.receive`. `configure_state_webhooks` creates the Webhooks.

Virtual Machine State gets three optional fields. They mirror Metal. Atlas keeps no restriction list. Layout: [Virtual Machine State](layouts.md#virtual-machine-state-layout-change).

| Field | Value |
| --- | --- |
| `network_desired_generation` | `desired.generation` of the VM in Metal |
| `network_observed_generation` | `observed.generation` of the VM in Metal |
| `network_error` | The message of `observed.error`, or empty |

- Metal adds the three values to each VM in the host sync response (`virtualMachineStateResponse` in [controller_sync.go](../../metal/internal/api/controller_sync.go)). An old Metal does not send them, and Atlas keeps the fields empty.
- The generations are the generations of the complete VM spec. A change that is not a network change also moves them.
- Host sync stores the values in `store_reported_states` ([vm_state.py](../../atlas/vm/core/vm_state.py)). Today it saves the document, and so runs the Webhook, only when `status` or `server` changes. It writes other values with one bulk update, which runs no Webhook. It must also save the document when one of the three fields changes.
- Host sync runs every 10 s. So Central learns of a change within about 10 s.

A third delivery in `DELIVERIES`:

| Item | Value |
| --- | --- |
| Event | `vm.network` |
| Document event | `on_update` |
| Condition | `network_desired_generation`, `network_observed_generation` or `network_error` changed |
| Payload | `event`, `virtual_machine`, `server`, `desired_generation`, `observed_generation`, `host_error`, `observed_at` |

```json
{
	"event": "vm.network",
	"virtual_machine": "vm-0a1b2c3d4e",
	"server": "metal-bom-01",
	"desired_generation": 42,
	"observed_generation": 42,
	"host_error": "",
	"observed_at": "2026-11-12 10:00:05"
}
```

- `DELIVERIES` is keyed by the document event today, and the Webhook name comes from that key. `vm.network` also uses `on_update`, so it needs its own key and its own Webhook name, for example `Virtual Machine State - Network - Central - 1`. It also needs its own payload. Today all deliveries share one payload.
- A rerun of `configure_state_webhooks` adds the Webhook.

So Central learns at once when:

- Metal applies a network change: the observed generation moves.
- Metal reports an error: `network_error` changes.
- An Atlas operator changes a restriction: the desired generation moves. See [operator emergency restriction](enforcement.md#operator-emergency-restriction).

Central handles `vm.network`: it reads `GET` VM and runs the same steps as its repair scan. The repair scan stays as the backup.

Deploy: Central ignores an unknown event (`unsupported event` in `central/integrations/state_delivery.py`). So Central ships the handler first. Then Atlas reruns `configure_state_webhooks`.

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
| `flow`, see [flow log](metrics.md#flow-log) | Metal host | The write key of the host |
| `sni`, see [SNI log](metrics.md#sni-log) | Metal host | The write key of the host |
| `nat-ranges`, see [NAT port ranges](metrics.md#nat-port-ranges) | Metal host | The write key of the host |
| `proxy-access` | HTTP proxy node | The write key of the proxy node |

- Each writer compresses its log with zstd and uploads one object every 5 min. The object key is in the [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout).
- A `nat-ranges` object holds the rows since the last upload. A host uploads it only when it has rows. The full table each day gives at least one object each day.
- Metal writes and uploads the `flow` and `sni` logs only when `metald.toml` has `[abuse] logs = true`. The default is false. Atlas writes the value from the Atlas Settings check `is_abuse_logs_enabled`, in the same way as `abuse_metrics`. Layout: [Atlas Settings](layouts.md#atlas-settings-layout-change).
- The `nat-ranges` log and the `proxy-access` upload do not need this switch. `nat-ranges` holds no customer traffic.
- Metal writes `nat-ranges` rows only when `metald.toml` has `[nat] is_port_range_enabled = true`. The default is false. Atlas writes the value from the Atlas Settings check `is_nat_port_range_enabled`, in the same way. See [NAT port ranges](metrics.md#nat-port-ranges).
- `metald.toml` has two optional values for the flow log: `[abuse] flow_map_entries`, default 262144, and `[abuse] flow_entries_per_vm`, default 10000. Atlas does not write them. A host without them uses the defaults.
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

The deploy order of all components, in three phases, is in [Central deploy order ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#deploy-order). These are the Atlas items of each phase, in order.

Phase 1: Flow log and counters. Data collection, and one data plane change: the NAT port ranges. No detection and no action. It starts after the Datum release and the Cargo log bucket.

1. The Atlas app: log write key provisioning, the Atlas Settings "Log Bucket" section, `is_abuse_metrics_enabled`, `is_abuse_logs_enabled` and `is_nat_port_range_enabled`. A Frappe patch makes a write key for each existing Metal Server and Proxy Server, and Atlas writes it into `metald.toml` and `proxy-control.toml` in the same way as at setup. Atlas placement puts no more VMs on a host than its [NAT slot](metrics.md#nat-port-ranges) count.
2. Metal: the counters behind `abuse_metrics`, the [flow log](metrics.md#flow-log) and the [SNI log](metrics.md#sni-log) behind `[abuse] logs`, the [NAT port ranges](metrics.md#nat-port-ranges) and their log behind `[nat] is_port_range_enabled`, and log upload. A metald patch gives a NAT slot to each existing VM.
3. The HTTP proxy: access log upload.
4. After the AUP and the privacy policy state the collection, an operator turns on `is_abuse_metrics_enabled`, `is_nat_port_range_enabled` and `is_abuse_logs_enabled`, in that order. Atlas Settings apply to every host in the region. After each switch, the operator checks host CPU, NAT errors and object sizes before the next switch. The operator checks the object sizes against the [storage estimates](metrics.md#flow-log).

Phase 2: Findings and restrictions. Central acts on sample findings, abuse reports and operator actions. There is no Watcher yet.

The [core service protection](protection.md) items do not need Phase 1 data. Each new nft rule ships in `count` mode. See [rollout](protection.md#rollout).

1. The Atlas app: the [restriction API](#restriction-api), the [mail ports API](#mail-ports-api), the `restrictions` and `is_mail_ports_blocked` create fields, and the tenant isolate Desk action. See [operator emergency restriction](enforcement.md#operator-emergency-restriction).
2. The Atlas app: the three Virtual Machine State fields and the `vm.network` delivery. See [events to Central](#events-to-central). Atlas reruns `configure_state_webhooks` only after Central ships the `vm.network` handler.
3. Metal: `restrictions` and `is_mail_ports_blocked` in the network object, the [`metal_abuse` chain](enforcement.md#metal_abuse-chain), the generations in host sync, the [VM guard](protection.md#vm-guard), the [per-VM conntrack limit](protection.md#conntrack), the [host baseline](protection.md#host-and-service-vm-baseline) sysctl values, and the host nft protection rules in `count` mode.
4. The service VMs (HTTP proxy, IPv6 router and WireGuard gateway): the baseline and the nft protection rules in `count` mode.
5. The HTTP proxy: the [tombstone](enforcement.md#http-proxy), the limit for each host, and the [handshake timeout](protection.md#tls-handshake-floods).
6. Insights: the [protection alerts](protection.md#counters-and-alerts).
7. The last Phase 2 step: an operator moves the protection rules to `drop`, one service at a time. See [rollout](protection.md#rollout).

Phase 3: Watcher. There is no Atlas change. Watcher reads the Phase 1 data.
