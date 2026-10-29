# Abuse protection data

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

## Records

| Record | Where | Retention |
| --- | --- | --- |
| eBPF counters | Metal memory | Until metald restarts |
| Metric samples for each VM | Metal JSONL | 7 days. This exists today. |
| Active circuit breakers | VM record directory on the host | Until the breaker ends |
| NAT log, SNI log, proxy access log | Regional Cargo bucket, tenant 0 | 180 days, by Cargo lifecycle rules |
| Abuse Signal, VM Restriction, Public IP Assignment | Atlas | 2 years (`record_retention_days` in Abuse Settings, 730 by default) |
| Evidence snapshot of a terminated VM | Atlas, tenant 0 | 30 days. Longer on legal hold. |
| Aggregate abuse metrics | Datum | No expiry. No tenant data. |

Central keeps cases and abuse reports. See the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

The Atlas cleanup job reads `record_retention_days` in Abuse Settings. It deletes the Atlas abuse records that are older. It does not delete an active restriction.

## Logs

| Log | Type | Writer | Content | Why |
| --- | --- | --- | --- | --- |
| NAT log | `nat` | Metal host | conntrack `NEW` and `DESTROY` events for VMs behind the host MASQUERADE | A complaint about a shared IPv4 address names only the address, port and time. The NAT log finds the VM. |
| SNI log | `sni` | Metal host | One row for each outbound TLS connection. See [SNI log](detection.md#sni-log). | Detection and evidence |
| Proxy access log | `proxy-access` | HTTP proxy node | OpenResty access lines | Attribution of site traffic. Today a node keeps it for hours only. |

Cargo owns the object layout of each log type. See the [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout). Shipping is in [log upload to Cargo](contract.md#log-upload-to-cargo). Atlas reads the current hour, which is not uploaded yet, from a Metal endpoint.

Keys:

- Metal hosts use the host write key.
- HTTP proxy nodes use the proxy write key.
- Atlas uses the read key.
- The key table is in [Cargo keys ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#keys).

Access:

- Central looks up logs through the attribution route and the evidence pack.
- Operators read raw logs through Atlas, with the read key. For an investigation that Atlas cannot answer, an operator gets a temporary read key from Cargo. See [Cargo access for investigation ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#access-for-investigation).
- SNI rows can be personal data. Keep them in the region. The AUP and the privacy policy must state the collection before the SNI log is switched on.

Evidence holds no payloads. Addresses that customers see are truncated to `/24`.

## Public IP history

Public IP Allocation holds only the current VM, and it has no `track_changes`. So Atlas loses the earlier holders of an address. See [public IPs](../networking/public-ips.md).

A new append-only Public IP Assignment record fixes this:

- One row for each attach. Atlas closes the row on detach.
- IPv6 addresses that Atlas gives to VMs get the same record.
- Attribution finds the row where `attached_at <= t < detached_at`, or where `detached_at` is empty.
- Each day without this record loses history, so it comes first.

Attribution methods:

| Method | Source |
| --- | --- |
| Public IP History | Public IP Assignment |
| NAT Log | NAT log rows for a shared IPv4 address |
| Proxy Route | HTTP proxy routes and access log, for a proxy address |

No match returns `unknown`. Atlas never guesses.

## Deletion

Atlas abuse records store `virtual_machine` as Data, not as a Link. So they stay after Atlas deletes the VM. Public IP Assignment links Public IP Allocation, because Atlas does not delete pool records with a VM.

## Time source

Log times must be traceable for CERT-In. No NIC or NPL time server is set today (`metal/cmd/metald/config.go`). Point chrony on hosts and service VMs at `samay1.nic.in`, `samay2.nic.in` and `time.nplindia.org`. Check the hostnames first. See the [Central CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md).

## Metrics and alerts

Atlas sends aggregates to Datum only, because Datum keeps series with no expiry. There is no series for each VM. The metrics are `atlas_abuse_signals_total{finding_code,severity}` and the Metal `metal_abuse_*` metrics in [telemetry](contract.md#telemetry), for example `metal_abuse_host_safety_triggers_total` and `metal_abuse_breakers_active`.

- Add [Insights alerts](../operate/dashboards.md#alerts) on these aggregates in `scripts/insights/alerts.py`. They post to Raven `#abuse`.
- Host safety opens no case and sends no event to Central. The alert on `metal_abuse_host_safety_triggers_total` is the only notice.
- A breaker storm on one host points to a detector bug or a host problem. Alert on it.
- Keep the queries of the [Abuse Protection ↗](https://dashboard.frappe.io/insights/dashboards/8eplc8khne) dashboard cheap. They share the live query limit with the Atlas alerts.
- Add a regional Atlas dashboard for detector health.
- `track_changes` on VM Restriction keeps the audit record of who restricted a VM and why, with the Central Resource Actions.
