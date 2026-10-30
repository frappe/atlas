# Abuse protection

Status: planned. Atlas does not implement this yet.

## Purpose

Abuse protection finds and limits abuse that starts from, or targets, tenant resources in a region. In Atlas, Metal exports per-VM metrics and logs, and enforces restrictions. The Atlas app puts the restrictions that Central applies in the VM network object in Metal. It keeps no restriction record.

Watcher, a Cargo module in each region, finds abuse and sends findings to Central. Finding codes, thresholds, feeds and calibration are in [Cargo Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md).

Central decides. Profiles, signals, cases, policy, reports and customer messages are in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher).

In scope for Atlas:

- Metrics and logs about VM traffic.
- Restrictions on VM traffic, and the port 25 block.
- Rate limits and the abuse tombstone on the HTTP proxy.
- Operator steps against inbound DDoS on tenants and on the platform.

Out of scope:

- Guest spoofing and traffic between tenants on the mesh. WG Mesh `local_vms` and `can_communicate` already stop them. See [WG Mesh](../networking/wg-mesh/index.md).
- An agent in the guest. Guests are untrusted.

## Principles

1. Metal and the HTTP proxy export metrics and logs. Watcher finds abuse. Central decides. Atlas keeps no thresholds, signals or cases, and it does not know teams, trust tiers or contacts.
2. Atlas restricts a VM only when Central calls the restriction API, or when an operator applies an emergency restriction.
3. Restrictions live in a layer that the tenant API cannot write. A tenant firewall, route or IPv4 change cannot widen them.
4. Count only the traffic that the guest starts. Never count replies.
5. Every restriction has an actor, an end condition and a case in Central.
6. Restrict traffic, never compute. The abuse system never pauses or stops a VM.
7. A failure of Central or Atlas must not block customers. Nothing new is applied, and restrictions still expire on the host.
8. Atlas stays backward compatible: optional fields, new routes and new eBPF maps only.

Reaction time is up to about 3 min from the start of abuse to a restriction, and no automatic limit exists before that. This is an accepted trade-off. See [Watcher thresholds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#thresholds).

## Who does what

| Component | Does |
| --- | --- |
| Metal, on each host | Per-VM eBPF counters, exported to Datum as `vm_abuse_*` metrics when `abuse_metrics` is on. NAT and SNI logs, uploaded to the Cargo log bucket every 5 min. The `metal_abuse` nft chain: `restrictions` and the `smtp_outbound` block for `is_port_25_blocked`, from the VM network object. |
| HTTP proxy | Static rate limits for each client address and site. Site tombstone. Access logs, uploaded to the Cargo log bucket every 5 min. |
| Atlas app, in each region | The `central_only` restriction and port 25 routes. Each one reads the network object from Metal, merges one change and replaces it. The restriction fields in `GET` VM. A write key from Cargo for each host and proxy node. `abuse_metrics` in host config. |
| Cargo | Log bucket, the Datum table for `vm_abuse_*` series, and the `watcher` read user. See [Cargo abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md). |
| Watcher, a Cargo module in each region | Reads Datum and the log store, matches feeds, and sends findings to Central. See [Cargo Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md). |
| Central | All decisions: profiles, cases and policy. It reads the restrictions back from `GET` VM in its repair scan. See [Central findings from Watcher ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher). |

```text
 Metal host                                          Cargo                                             Central
 counters ─────── vm_abuse_* metrics, every 30 s ──► Datum per-VM table ──┐
 NAT and SNI log ─┬── objects, every 5 min ────────► log bucket ──────────┼──► Watcher ── findings ──► cases, policy
 proxy access log ┘                         blocklists, abuse databases ──┘                             │
                                                                                                        │ restriction and
 Metal host                                  Atlas app                                                  │ port 25 API
 VM network object ◄── PUT .../network ───── read, merge, replace ◄─────────────────────────────────────┘
 metal_abuse chain ─── desired network, ───► GET VM ─── restrictions, generations ──► Central repair scan
                       generations
```

## Data

Atlas stores no abuse record. The data is in these places:

| Data | Place | Retention |
| --- | --- | --- |
| Restrictions and `is_port_25_blocked` | The VM network object in Metal. See [restrictions in the network object](enforcement.md#restrictions-in-the-network-object). | Until the entry is lifted. Metal skips an entry after its `expires_at`. |
| eBPF counters | Memory of metald. See [counters](metrics.md#counters). | Until metald restarts |
| `vm_abuse_*` metrics | Datum. See [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics). | 30 days, by the Datum table TTL |
| NAT, SNI and proxy access logs | The Cargo log bucket. See [log upload](contract.md#log-upload). | 180 days, by the bucket lifecycle rules. See [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout). |
| Restriction history, cases, signals, reports and public IP history | Central. See [Central restriction states ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#restriction-states). | Central rules |
| Emergency restrictions | A Comment on the Atlas Virtual Machine, and a row in Central | Like other Comments |

## Pages

| Page | Content |
| --- | --- |
| [Metrics](metrics.md) | Per-VM counters, port classes, SNI log, NAT log, baseline script |
| [Enforcement](enforcement.md) | Restrictions in the network object, `metal_abuse` chain, restriction kinds, operator emergency restriction, HTTP proxy, inbound DDoS |
| [Contract](contract.md) | Metal and Atlas, restriction API, port 25 API, telemetry, log upload, log bucket keys, time source |
| [Layouts](layouts.md) | Layout mockups. They wait for engineer approval. |

Cargo pages:

- [Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md): inputs, finding codes, thresholds, feeds, findings, attribution, calibration.
- [Abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md): log bucket, keys, per-VM metrics table.

Central pages:

- [Abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md): findings from Watcher, profiles, cases, ladder, policy, lifts, reports, customer messages, deploy order.
- [CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md): reporting tiers, clock and workflow.
- [Abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md): Central DocType mockups.
