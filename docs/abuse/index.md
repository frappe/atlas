# Abuse protection

Status: planned. Atlas does not implement this yet.

## Purpose

Abuse protection finds and limits abuse that starts from, or targets, tenant resources in a region. In Atlas, Metal exports per-VM metrics and logs, and enforces restrictions. The Atlas app puts the restrictions that Central applies in the VM network object in Metal. It keeps no restriction record.

Watcher, a Cargo module in each region, finds abuse and sends findings to Central. Finding codes, thresholds, feeds and calibration are in [Cargo Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md).

Central decides. Profiles, signals, cases, policy, reports and customer messages are in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher).

In scope for Atlas:

- Metrics and logs about VM traffic.
- Restrictions on VM traffic, and the mail ports block.
- Events to Central when the network of a VM changes on the host.
- Rate limits and the abuse tombstone on the HTTP proxy.
- Operator steps against inbound DDoS on tenants and on the platform.
- Protection of Metal hosts and Atlas service VMs against floods, scans and spoofed packets. See [core service protection](protection.md).

Out of scope:

- Traffic between tenants on the mesh. WG Mesh `local_vms` and `can_communicate` already stop it. See [WG Mesh](../networking/wg-mesh/index.md). The [VM guard](protection.md#vm-guard) adds source checks at the VM tap.
- An agent in the guest. Guests are untrusted.

## Principles

1. Metal and the HTTP proxy export metrics and logs. Watcher finds abuse. Central decides. Atlas keeps no thresholds, signals or cases, and it does not know teams, trust tiers or contacts.
2. Atlas restricts a VM only when Central calls the restriction API, or when an operator applies an emergency restriction to a VM or a tenant. The static [protection rules](protection.md) are not restrictions.
3. Restrictions live in a layer that the tenant API cannot write. A tenant firewall, route or IPv4 change cannot widen them.
4. Count only the traffic that the guest starts. Never count replies.
5. Every restriction has an actor, an end condition and a case in Central.
6. Restrict traffic, never compute. The abuse system never pauses or stops a VM.
7. A failure of Central or Atlas must not block customers. Nothing new is applied, and restrictions still expire on the host.
8. Atlas stays backward compatible: optional fields, new routes and new eBPF maps only.

From Phase 3, when the thresholds are Active, reaction time is up to about 3 min from the start of abuse to a restriction. No automatic limit exists before the restriction. This is an accepted trade-off. See [Watcher thresholds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#thresholds).

## Who does what

| Component | Does |
| --- | --- |
| Metal, on each host | Per-VM eBPF counters, exported to Datum as `vm_abuse_*` metrics when `abuse_metrics` is on. Flow, SNI and NAT port range logs, uploaded to the Cargo log bucket every 5 min. A fixed source port range for each VM on the shared host address. The `metal_abuse` nft chain: `restrictions` and the `mail_ports` block for `is_mail_ports_blocked`, from the VM network object. The generations and the error of each VM in host sync. The VM guard, the per-VM conntrack limit, the sysctl baseline and the host nft protection rules. |
| HTTP proxy | Static rate limits for each client address, site and host. Site tombstone. Access logs, uploaded to the Cargo log bucket every 5 min. |
| Service VMs: HTTP proxy, IPv6 router, WireGuard gateway | The sysctl baseline and the nft protection rules. See [core service protection](protection.md). |
| Atlas app, in each region | The `central_only` restriction and mail ports routes. Each one reads the network object from Metal, merges one change and replaces it. The restriction fields in `GET` VM. The `vm.network` event to Central, from three Virtual Machine State fields. The emergency restriction and tenant isolate Desk actions. A write key from Cargo for each host and proxy node. `abuse_metrics`, `[abuse] logs`, `[nat] is_port_range_enabled` and `[protect] mode` in host config. |
| Cargo | Log bucket, the Datum table for `vm_abuse_*` series, and the `watcher` read user. See [Cargo abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md). |
| Watcher, a Cargo module in each region | Reads Datum and the log store, matches feeds, and sends findings to Central. See [Cargo Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md). |
| Insights | The protection alerts, in `scripts/insights/alerts.py`. See [counters and alerts](protection.md#counters-and-alerts). |
| Central | All decisions: profiles, cases and policy. It reads the restrictions back from `GET` VM when it gets a `vm.network` event, and in its repair scan. See [Central findings from Watcher ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher). |

```text
 Metal host                                          Cargo                                             Central
 counters ─────── vm_abuse_* metrics, every 30 s ──► Datum per-VM table ──┐
 flow, SNI, NAT ──┬── objects, every 5 min ────────► log bucket ──────────┼──► Watcher ── findings ──► cases, policy
 proxy access log ┘                         blocklists, abuse databases ──┘                             │
                                                                                                        │ restriction and
 Metal host                                  Atlas app                                                  │ mail ports API
 VM network object ◄── PUT .../network ───── read, merge, replace ◄─────────────────────────────────────┘
 metal_abuse chain ─── desired network, ───► GET VM ─── restrictions, generations ──► Central repair scan
                       generations
 host sync ─────── generations, error ─────► Virtual Machine State ─── vm.network ──► Central handler
```

## Data

Atlas stores no abuse record. The data is in these places:

| Data | Place | Retention |
| --- | --- | --- |
| Restrictions and `is_mail_ports_blocked` | The VM network object in Metal. See [restrictions in the network object](enforcement.md#restrictions-in-the-network-object). | Until the entry is lifted. Metal skips an entry after its `expires_at`. |
| Network generations and error of each VM | Virtual Machine State in Atlas, copied from Metal by host sync. See [events to Central](contract.md#events-to-central). | Until the VM is deleted |
| Protection counters, `atlas_protect_*` | Datum. See [counters and alerts](protection.md#counters-and-alerts). | Datum rules for other series |
| Port scan sources | An nft set on each host and service VM. See [inbound port scans](protection.md#inbound-port-scans). | 1 h for each source |
| eBPF counters and flow maps | Memory of metald. See [counters](metrics.md#counters) and [flow log](metrics.md#flow-log). | Until metald restarts. metald deletes a flow entry after 2 min with no packet. |
| `vm_abuse_*` metrics | Datum. See [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics). | 30 days, by the Datum table TTL |
| Flow, SNI, NAT port range and proxy access logs | The Cargo log bucket. See [log upload](contract.md#log-upload). | 180 days, by the bucket lifecycle rules. See [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout). |
| Restriction history, cases, signals, reports and public IP history | Central. See [Central restriction states ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#restriction-states). | Central rules |
| Emergency restrictions | A Comment on the Atlas Virtual Machine, and a row in Central | Like other Comments |

## Pages

| Page | Content |
| --- | --- |
| [Metrics](metrics.md) | Per-VM counters, port classes, SNI log, flow log, NAT port ranges, baseline script |
| [Enforcement](enforcement.md) | Restrictions in the network object, `metal_abuse` chain, restriction kinds, operator emergency restriction, HTTP proxy, inbound DDoS |
| [Core service protection](protection.md) | Scope, rollout, host and service VM baseline, SYN, UDP, ICMP and TLS handshake floods, conntrack, spoofed sources, port scans, fragments, WireGuard, IPv6 router, VM guard, counters and alerts |
| [Contract](contract.md) | Metal and Atlas, restriction API, mail ports API, events to Central, telemetry, log upload, log bucket keys, time source, deploy order |
| [Layouts](layouts.md) | Layout mockups. They wait for engineer approval. |

Cargo pages:

- [Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md): inputs, finding codes, thresholds, feeds, findings, attribution, calibration.
- [Abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md): log bucket, keys, per-VM metrics table.

Central pages:

- [Abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md): findings from Watcher, profiles, cases, ladder, policy, lifts, reports, customer messages, deploy order.
- [CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md): reporting tiers, clock and workflow.
- [Abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md): Central DocType mockups.
