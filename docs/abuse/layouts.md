# Abuse protection layouts

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

These are mockups for the new Atlas DocTypes and the Virtual Machine layout change. **They wait for engineer approval.** Do not write DocType JSON before the approval. The layout rules are in the [engineering playbook](../../llm/engineering-playbook/references/code.md#doctype-and-dialog-layout). The Central mockups are in [Central abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md).

Notation: `[Section]`, `|` between columns and `{Tab}`. Each field shows the label, the fieldname and the type. RO means read only.

All Atlas abuse records name the VM with a Data field `virtual_machine`, not a Link, so the history stays after VM deletion. See [deletion](data.md#deletion).

Roles: `Atlas Admin` writes and `Atlas Investigator` reads. There is no new Atlas role, because Central makes the abuse decisions about customers.

Decided:

1. Default blocks are host rules. `smtp_outbound` and `mining_pools` render in `metal_abuse` for every VM, with no VM Restriction rows. `is_port_25_allowed` on the VM is the only exception. Central sets it when it accepts an unblock. `mining_pools` takes no exception.
2. `mode` (Off, Shadow, Active) for each finding code on Abuse Threshold is the calibration switch.

### Abuse Signal

Append only. Naming: UUID, like Public IP Allocation. `track_changes` is on, for the review fields.

```text
┌ Abuse Signal ─────────────────────────────────────────────────────────────────────────────┐
│ [Signal]                                                                                  │
│ Finding Code      finding_code      Data  RO    │ Virtual Machine  virtual_machine  Data RO │
│ Severity          severity          Select RO   │ Tenant ID        tenant_id        Int  RO │
│   Low / Medium / High / Critical                │ Metal Server     metal_server     Link RO │
│ Source            source            Select RO   │                                         │
│   Metal / Proxy / Feed / Operator               │                                         │
│                                                                                           │
│ [Window]                                                                                  │
│ Window Start      window_start      Datetime RO │ Count            count            Int  RO │
│ Window End        window_end        Datetime RO │ Sent to Central  sent_to_central_at  DT RO│
│                                                                                           │
│ [Circuit Breaker]                                                                         │
│ Circuit Breaker   circuit_breaker   Data RO     │ Breaker Expires  breaker_expires_at DT RO │
│   e.g. "port_class:ssh"                         │                                         │
│                                                                                           │
│ [Review]                                                                                  │
│ Shadow            is_shadow         Check RO    │ Reviewed By      reviewed_by      Link    │
│ False Positive    is_false_positive Check       │ Review Note      review_note   Small Text │
│                                                                                           │
│ [Evidence]  (collapsed)                                                                   │
│ Evidence          evidence          JSON RO                                               │
│   counts, rates, top destination ports, destination prefixes truncated to /24             │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- `sent_to_central_at` is empty for a signal that stays in Atlas: below the minimum severity, or shadow.
- Only the Review section is writable. Only `Atlas Admin` can write it.
- Indexes: (`virtual_machine`, `window_start`), (`finding_code`, `window_start`).

### VM Restriction

Desired state. Naming: UUID. `track_changes` is on.

```text
┌ VM Restriction ───────────────────────────────────────────────────────────────────────────┐
│ [Restriction]                                                                             │
│ Virtual Machine virtual_machine Data RO      │ Status          status          Select RO  │
│ Tenant ID       tenant_id       Int RO       │   Pending / Applied / Lifted / Failed      │
│ Kind            kind            Select RO    │ Expires At      expires_at      Datetime   │
│   egress_throttle / port_block /             │ Applied on Host host_applied_at Datetime RO│
│   quarantine / isolate                       │                                            │
│                                                                                           │
│ [Reason]                                                                                  │
│ Reason Code     reason_code     Data RO      │ Case Reference  case_reference  Data RO    │
│   finding code or "operator"                 │   Central case, e.g. AC-00192              │
│ Idempotency Key idempotency_key Data RO      │                                            │
│   Central Resource Action, unique            │                                            │
│                                                                                           │
│ [Applied]                                                                                 │
│ Applied By      applied_by      Select RO    │ Lifted At       lifted_at       Datetime RO│
│   Central / Customer / Operator              │ Lifted By       lifted_by       Select RO  │
│ Applied By User applied_by_user Link User RO │   Central / Customer / Operator / Expiry   │
│ Applied At      applied_at      Datetime RO  │                                            │
│                                                                                           │
│ [Parameters]  (collapsed)                                                                 │
│ Parameters      parameters      JSON RO                                                   │
│   e.g. {"port_classes": ["smtp"], "ports": [25]} or {"pps": 5000}                         │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- `status` replaces the earlier `is_active` sketch. The values are the `status` values of the `vm.restriction` event. See [who changes restrictions](enforcement.md#who-changes-restrictions). `lifted_by` keeps the reason that a restriction ended.
- `kind` uses the API values, so the event and the API need no mapping.
- `idempotency_key` holds the name of the Central Resource Action. It has a unique index. A repeat apply call with the same key returns this row. See [apply a restriction](contract.md#apply-a-restriction).
- For `port_block`, Atlas resolves the port classes in the request to ports and keeps both in `parameters`.
- Atlas sets `host_applied_at` when Metal reports that it rendered the rule. During an incident, it shows the gap between desired and observed state.
- Only `Atlas Admin` creates a row in Desk. This is the [emergency path](enforcement.md#who-changes-restrictions). Atlas sets `applied_by = Operator` and `applied_by_user` itself.

### Public IP Assignment

Append only. Naming: UUID. One row for each attach.

```text
┌ Public IP Assignment ─────────────────────────────────────────────────────────────────────┐
│ [Address]                                                                                 │
│ Address           address           Data RO     │ Version          version        Select RO │
│ Allocation        allocation        Link RO     │   4 / 6                                   │
│   Public IP Allocation                          │                                           │
│                                                                                           │
│ [Holder]                                                                                  │
│ Virtual Machine   virtual_machine   Data RO     │ Attached At      attached_at    Datetime RO│
│ Tenant ID         tenant_id         Int  RO     │ Detached At      detached_at    Datetime RO│
│ Metal Server      metal_server      Link RO     │                                           │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- Index: (`address`, `attached_at`). Attribution finds the row where `attached_at <= t < detached_at`, or where `detached_at` is empty.
- `allocation` is a Link, because allocations are pool records. Atlas does not delete them with the VM.

### Abuse Settings

Single. Fields that hold credentials are Password.

```text
┌ Abuse Settings ───────────────────────────────────────────────────────────────────────────┐
│ {Detection}                                                                               │
│ [Thresholds]                                                                              │
│ Thresholds        thresholds        Table  Abuse Threshold                                │
│                                                                                           │
│ [Profiles]                                                                                │
│ Strict Multiplier          strict_multiplier          Float  0.5 │ Relaxed Multiplier      │
│ Strict Duration Multiplier strict_duration_multiplier Float  2   │  relaxed_multiplier     │
│                                                                  │  Float 2                │
│ [Circuit Breakers]                                                                        │
│ First Duration (Minutes)  breaker_first_minutes   Int 30 │ Release Below (%)              │
│ Maximum Duration (Hours)  breaker_maximum_hours   Int 24 │  breaker_release_percent Int 50│
│                                                          │ Release After (Minutes)        │
│                                                          │  breaker_release_minutes Int 10│
│ [Port Classes]                                                                            │
│ Port Classes      port_classes      Table  Abuse Port Class                               │
│                                                                                           │
│ {Delivery}                                                                                │
│ [Signals to Central]                                                                      │
│ Minimum Severity Sent      minimum_severity_sent   Select Medium │ Always Sent Codes       │
│                                                                  │  always_sent_finding_codes│
│                                                                  │  Small Text, one per line│
│ [Retention]                                                                               │
│ Keep Records (Days)        record_retention_days   Int 730       │                         │
│                                                                                           │
│ {Feeds}                                                                                   │
│ Feeds             feeds             Table  Abuse Feed                                     │
│                                                                                           │
│ {Restrictions}                                                                            │
│ [Quarantine]                                                                              │
│ Package Sources   package_sources   Table  Abuse Package Source                           │
│                                                                                           │
│ {Log Bucket}                                                                              │
│ [Bucket]                                                                                  │
│ Endpoint    log_bucket_endpoint   Data       │ Bucket Name  log_bucket_name  Data         │
│                                                                                           │
│ [Read Key]  Atlas reads the logs with it                                                  │
│ Key ID  read_access_key_id         Data      │ Secret Key read_secret_access_key          │
│                                              │            Password                        │
│ [Host Write Key]  Atlas writes it into metald host config                                 │
│ Key ID  host_write_access_key_id   Data      │ Secret Key host_write_secret_access_key    │
│                                              │            Password                        │
│ [Proxy Write Key]  Atlas writes it into proxy node config                                 │
│ Key ID  proxy_write_access_key_id  Data      │ Secret Key proxy_write_secret_access_key   │
│                                              │            Password                        │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

Child tables:

| Child | Fields |
| --- | --- |
| Abuse Threshold | `finding_code` Data, `parameter` Data (for example `sent_pps`), `value` Float, `unit` Data, `sustain_seconds` Int, `mode` Select (Off, Shadow, Active) |
| Abuse Port Class | `port_class` Data (auth, smtp, submission, amplification, mining), `ports` Data (for example `22,3389,5432`) |
| Abuse Feed | `feed_name` Data, `kind` Select (Mining Pools, DNSBL, Shadowserver, SNDS, Spamhaus DROP, abuse.ch, Tor, Safe Browsing, OpenPhish, URLhaus), `url` Data, `credential` Password, `is_enabled` Check, `last_fetched_at` Datetime RO, `last_error` Small Text RO |
| Abuse Package Source | `hostname` Data |

- `mode` on each threshold is the switch for [calibration](detection.md#calibration). Shadow writes signals with `is_shadow`. Active also applies breakers. So an operator turns on breakers one finding code at a time, with no deploy.
- The defaults in the mockup, such as 0.5, 30 and 50, are the [starting values](detection.md#thresholds). The threshold values ship as fixtures, not as JSON defaults, so a region can use different values.
- `record_retention_days` sets how long Atlas keeps its abuse records. The Atlas cleanup job reads it. See [records](data.md#records).
- An operator copies the log bucket endpoint, the bucket name and the keys from Cargo. See [Cargo keys ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#keys).
- Atlas uses the read key. It writes the host write key into the metald host config, and the proxy write key into the proxy node config. See [log upload to Cargo](contract.md#log-upload-to-cargo).

### Virtual Machine (layout change)

A new collapsed section "Abuse" after "Network". All fields are optional. Atlas sets the `infrastructure` profile on privileged tenant-0 VMs. All other abuse fields come from Central through the API. Atlas refuses `infrastructure` in a Central request.

```text
│ [Abuse]  (collapsed)                                                                      │
│ Abuse Profile   abuse_profile      Select RO │ Open Abuse Case has_open_abuse_case        │
│   strict / standard / relaxed /              │                 Check RO                   │
│   infrastructure                             │                                            │
│ Port 25 Allowed is_port_25_allowed Check RO  │                                            │
│ Overrides       abuse_overrides    Table RO  VM Abuse Override                            │
```

| Child | Fields |
| --- | --- |
| VM Abuse Override | `finding_code` Data, `multiplier` Float, `is_disabled` Check, `expires_at` Datetime, `exception_reference` Data (Central Abuse Exception) |

- An empty `abuse_profile` means `standard`, so old VMs keep working.
- `is_port_25_allowed` is the only exception to the default blocks. See decision 1 above.

