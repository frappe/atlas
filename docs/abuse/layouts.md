# Abuse protection layouts

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

These are mockups for the Atlas layout changes. There is no new Atlas DocType. **They wait for engineer approval.** Do not write DocType JSON before the approval. The layout rules are in the [engineering playbook](../../llm/engineering-playbook/references/code.md#doctype-and-dialog-layout). The Central mockups are in [Central abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md).

Notation: `[Section]`, `|` between columns and `{Tab}`. Each field shows the label, the fieldname and the type. RO means read only. Virtual means that the controller reads the value from Metal, like `routes` today.

Roles: `Atlas Admin` writes and `Atlas Investigator` reads. There is no new Atlas role, because Central makes the abuse decisions about customers.

Decided:

1. Restrictions and `is_mail_ports_blocked` live in the Metal VM network object. Atlas keeps no restriction record. See [restrictions in the network object](enforcement.md#restrictions-in-the-network-object).
2. The `mail_ports` block renders in `metal_abuse` only for a VM whose network has `is_mail_ports_blocked`. A missing field means open. Central sets the field on create, and with the [mail ports API](contract.md#mail-ports-api).
5. Virtual Machine State mirrors three network values from Metal for the [`vm.network` event](contract.md#events-to-central). It holds no restriction list.
3. Atlas has no detection settings. Thresholds and feeds are in [Watcher thresholds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#thresholds). Profiles and exceptions are in [Central profiles ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#profiles).
4. Atlas gets the log bucket write keys from Cargo. No operator copies a key. See [log bucket keys](contract.md#log-bucket-keys).

### Atlas Settings (layout change)

A new section "Log Bucket" in the "Storage" tab, after "Object Storage".

```text
{Storage}
│ [Object Storage]  (no change)                                                             │
│                                                                                           │
│ [Log Bucket]                                                                              │
│ Bucket          log_bucket_name          Data │ Export Abuse Metrics                       │
│ Endpoint URL    log_bucket_endpoint_url  Data │   is_abuse_metrics_enabled  Check          │
│                                               │   writes [telemetry] abuse_metrics         │
│                                               │ Record Abuse Logs                          │
│                                               │   is_abuse_logs_enabled  Check             │
│                                               │   writes [abuse] logs                      │
│                                               │ Use NAT Port Ranges                        │
│                                               │   is_nat_port_range_enabled  Check         │
│                                               │   writes [nat] is_port_range_enabled       │
```

- The fieldnames follow the `object_storage_*` fields.
- `is_abuse_metrics_enabled` is off by default. An operator turns it on after the Datum release. See [telemetry](contract.md#telemetry).
- `is_abuse_logs_enabled` is off by default. Metal records and uploads the `flow` and `sni` logs only when it is on. An operator turns it on only after the AUP and the privacy policy state the collection. See [log upload](contract.md#log-upload).
- `is_nat_port_range_enabled` is off by default. It changes live NAT on the host. See [NAT port ranges](metrics.md#nat-port-ranges).
- An operator turns on `is_abuse_metrics_enabled`, `is_nat_port_range_enabled` and `is_abuse_logs_enabled`, in that order. See [deploy order](contract.md#deploy-order).
- Atlas Settings holds no log bucket key. The keys are on each Metal Server and Proxy Server.

### Atlas Settings: Protection (layout change)

A new section "Protection" in the "Networking" tab, after "Warpgate".

```text
{Networking}
│ [Protection]                                                                              │
│ Metal Hosts                                   │ IPv6 Router                                │
│   protect_mode_metal  Select                  │   protect_mode_ipv6_router  Select         │
│ HTTP Proxy                                    │ WireGuard Gateway                          │
│   protect_mode_http_proxy  Select             │   protect_mode_wireguard_gateway  Select   │
│                                               │   options for all four: count / drop       │
```

- Each field is `count` by default. See [rollout](protection.md#rollout).
- Atlas writes `protect_mode_metal` to `[protect] mode` in `metald.toml` on each host, and each other field to `[protect] mode` in the config of that service.
- An operator sets `drop` for one service at a time, after at least 7 days of counters.

### Metal Server and Proxy Server (layout change)

A new collapsed section "Log Bucket" on both DocTypes. On Metal Server it goes after "Wireguard". On Proxy Server it goes after "Runtime".

```text
│ [Log Bucket]  (collapsed)                                                                 │
│ Access Key ID                                 │ Key Rotated On                             │
│   log_bucket_access_key_id  Data RO           │   log_bucket_key_rotated_on  Datetime RO   │
│ Secret Access Key                             │                                            │
│   log_bucket_secret_access_key  Password RO   │                                            │
```

- Atlas sets all three fields when it creates or rotates the key. See [log bucket keys](contract.md#log-bucket-keys).
- The key can write but not read. Atlas has no read key.

### Virtual Machine State (layout change)

A new section "Network" after "Sleep". Host sync copies the three fields from Metal. See [events to Central](contract.md#events-to-central).

```text
│ [Network]                                                                                 │
│ Desired Generation                            │ Network Error                              │
│   network_desired_generation  Int RO          │   network_error  Small Text RO             │
│ Observed Generation                           │                                            │
│   network_observed_generation  Int RO         │                                            │
```

- The fields mirror `desired.generation`, `observed.generation` and the message of `observed.error` of the VM in Metal.
- A change of one of them runs the `vm.network` Webhook.
- A host with an old Metal leaves them empty.

### Virtual Machine (layout change)

Two new virtual fields, both read only. Atlas reads them from Metal, like `routes`.

```text
│ [Network]                                                                                 │
│ ...                                           │ ...                                        │
│                                               │ Accessible via WireGuard Gateway           │
│                                               │ Mail Ports Blocked                         │
│                                               │   is_mail_ports_blocked  Check RO virtual  │
│                                                                                           │
│ [ ]  (the section with routes)                                                            │
│ Routes          routes          Code RO virtual                                           │
│ SSH Keys        ssh_keys        Small Text RO virtual                                     │
│ Firewall        firewall_summary Code RO virtual                                          │
│ Restrictions    restrictions    Code RO virtual                                           │
│   the restrictions list from Metal: id, kind, parameters, case reference, expires at      │
│ Metadata        metadata        Code RO virtual                                           │
```

- The tenant API and Desk cannot edit either field. Only the [restriction API](contract.md#restriction-api), the [mail ports API](contract.md#mail-ports-api) and the emergency actions change them.
- An expired entry stays in `restrictions` until the next change of the VM network. Metal already skips it.

Desk actions in a new "Abuse" group, for `Atlas Admin` only. See [operator emergency restriction](enforcement.md#operator-emergency-restriction).

```text
┌ Apply Emergency Restriction ──────────────────────────────────────────────────────────────┐
│ Kind            kind            Select       │ Expires At      expires_at      Datetime   │
│   egress_throttle / ingress_throttle /       │                                            │
│   port_block / quarantine / isolate          │                                            │
│ Ports           ports           Small Text   │ PPS             pps             Int        │
│   port_block only, e.g. 25/tcp, 465/tcp      │ BPS             bps             Int        │
│                                              │   throttles only                           │
│ Reason          reason          Small Text, required                                      │
│                                                              [Cancel]  [Apply Restriction]│
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- Atlas makes the entry ID: `atlas-` and a random suffix.
- Atlas adds a Comment on the VM with the user, the kind and the reason.
- "Lift Restriction" asks for one entry ID from `restrictions` and a reason. It lifts that entry only, and adds a Comment.

"Isolate Tenant" is a Desk action on the Virtual Machine list, in the same "Abuse" group, for `Atlas Admin` only. See [operator emergency restriction](enforcement.md#operator-emergency-restriction).

```text
┌ Isolate Tenant ───────────────────────────────────────────────────────────────────────────┐
│ Tenant ID       tenant_id       Int          │ Expires At      expires_at      Datetime   │
│   required, not 0                            │                                            │
│ Reason          reason          Small Text, required                                      │
│                                                                 [Cancel]  [Isolate Tenant]│
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- Atlas refuses tenant 0.
- Atlas makes one entry ID, `atlas-` and a random suffix, and applies an `isolate` entry with that ID to each VM of the tenant, one VM at a time, in a background job.
- Atlas adds a Comment on each VM with the user, the entry ID and the reason.
- "Lift Tenant Isolation" asks for the tenant ID, the entry ID and a reason. It lifts that entry on each VM of the tenant, and adds a Comment on each VM.
