# Abuse protection layouts

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

These are mockups for the new Atlas DocTypes and the Virtual Machine layout change. **They wait for engineer approval.** Do not write DocType JSON before the approval. The layout rules are in the [engineering playbook](../../llm/engineering-playbook/references/code.md#doctype-and-dialog-layout). The Central mockups are in [Central abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md).

Notation: `[Section]`, `|` between columns and `{Tab}`. Each field shows the label, the fieldname and the type. RO means read only.

Roles: `Atlas Admin` writes and `Atlas Investigator` reads. There is no new Atlas role, because Central makes the abuse decisions about customers.

Decided:

1. `smtp_outbound` is the only default block. It renders in `metal_abuse` for every VM, with no VM Restriction row. `is_port_25_allowed` on the VM is its only exception. Central sets it when it accepts an unblock.
2. Atlas has no detection settings. Thresholds, profiles, feeds and exceptions are in [Central detection ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#detection).

### VM Restriction

Desired state. Naming: UUID. `track_changes` is on.

```text
┌ VM Restriction ───────────────────────────────────────────────────────────────────────────┐
│ [Restriction]                                                                             │
│ Virtual Machine virtual_machine Data RO      │ Status          status          Select RO  │
│ Tenant ID       tenant_id       Int RO       │   Pending / Applied / Lifted / Failed      │
│ Kind            kind            Select RO    │ Expires At      expires_at      Datetime   │
│   egress_throttle / ingress_throttle /       │ Applied on Host host_applied_at Datetime RO│
│   port_block / quarantine / isolate          │                                            │
│                                                                                           │
│ [Reason]                                                                                  │
│ Case Reference  case_reference  Data RO      │ Idempotency Key idempotency_key Data RO    │
│   Central case, e.g. AC-00192                │   Central Resource Action, unique          │
│                                                                                           │
│ [Applied]                                                                                 │
│ Applied By      applied_by      Select RO    │ Lifted At       lifted_at       Datetime RO│
│   Central / Operator                         │ Lifted By       lifted_by       Select RO  │
│ Applied By User applied_by_user Link User RO │   Central / Expiry / Atlas user            │
│ Applied At      applied_at      Datetime RO  │                                            │
│                                                                                           │
│ [Parameters]  (collapsed)                                                                 │
│ Parameters      parameters      JSON RO                                                   │
│   e.g. {"ports": [{"port": 25, "protocol": "tcp"}]} or {"pps": 5000}                      │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- The `status` values are the `status` values of the `vm.restriction` event. See [restriction states](enforcement.md#restriction-states). `lifted_by` keeps the reason that a restriction ended. Its values are the `lifted_by` values of the event.
- `kind` uses the API values, so the event and the API need no mapping.
- `idempotency_key` holds the name of the Central Resource Action. It has a unique index. A repeat apply call with the same key returns this row. See [restriction API](contract.md#restriction-api).
- For `port_block`, `parameters` holds the explicit ports from the request. Atlas does not know port classes.
- Atlas sets `host_applied_at` when Metal reports that it rendered the rule. During an incident, it shows the gap between desired and observed state.
- Only `Atlas Admin` creates a row in Desk. This is the emergency path in [restriction states](enforcement.md#restriction-states). Atlas sets `applied_by = Operator` and `applied_by_user` itself.
- `virtual_machine` is Data, not a Link, so the row stays after VM deletion. See [records](data.md#records).

### Abuse Settings

Single. Fields that hold credentials are Password.

```text
┌ Abuse Settings ───────────────────────────────────────────────────────────────────────────┐
│ [Log Bucket]                                                                              │
│ Endpoint  log_bucket_endpoint  Data          │ Bucket Name  log_bucket_name  Data         │
│ Host Write Key ID                            │ Proxy Write Key ID                         │
│   host_write_access_key_id  Data             │   proxy_write_access_key_id  Data          │
│ Host Write Secret                            │ Proxy Write Secret                         │
│   host_write_secret_access_key  Password     │   proxy_write_secret_access_key  Password  │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- An operator copies the endpoint, the bucket name and the write keys from Cargo. See [Cargo keys ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#keys).
- Atlas writes the host write key into the metald host config, and the proxy write key into the proxy node config. See [log upload](contract.md#log-upload).
- Atlas has no read key.

### Virtual Machine (layout change)

A new collapsed section "Abuse" after "Network". All fields are optional and read only. Only Central sets them, through the API.

```text
│ [Abuse]  (collapsed)                                                                      │
│ Port 25 Allowed is_port_25_allowed Check RO  │ Restrictions    restrictions_html  HTML RO │
│                                              │   active VM Restriction rows: kind,        │
│                                              │   status, case reference, expires at       │
```

- `is_port_25_allowed` is the only exception to the default block. See decision 1 above.
- `restrictions_html` lists the active VM Restriction rows of the VM, with a link to each row. VM Restriction uses a Data field for the VM, so Desk cannot show it as a connection.
