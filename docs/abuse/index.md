# Abuse protection

Status: planned. Atlas does not implement this yet.

## Purpose

Abuse protection finds and limits abuse that starts from, or targets, tenant resources in a region. In Atlas, Metal exports per-VM metrics and logs, and enforces restrictions. The Atlas app keeps the restrictions that Central applies.

Watcher, a Cargo module in each region, finds abuse and sends findings to Central. Finding codes, thresholds, feeds and calibration are in [Cargo Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md).

Central decides. Profiles, signals, cases, policy, reports and customer messages are in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher).

In scope for Atlas:

- Metrics and logs about VM traffic.
- Restrictions on VM traffic, and the port 25 default block.
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
8. Atlas stays backward compatible: optional fields, optional events and new eBPF maps only.

Reaction time is up to about 3 min from the start of abuse to a restriction, and no automatic limit exists before that. This is an accepted trade-off. See [Watcher thresholds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#thresholds).

## Who does what

| Component | Does |
| --- | --- |
| Metal, on each host | Per-VM eBPF counters, exported to Datum as `vm_abuse_*` metrics. NAT and SNI logs, uploaded to the Cargo log bucket every 5 min. The `metal_abuse` nft chain: restrictions from the VM config, and the default block `smtp_outbound`. |
| HTTP proxy | Static rate limits for each client address and site. Site tombstone. Access logs, uploaded to the Cargo log bucket every 5 min. |
| Atlas app, in each region | VM Restriction and the `central_only` restriction API. `is_port_25_allowed` on the VM. The `vm.restriction` event to Central. `restriction_status` in host sync. The host and proxy write keys in host and proxy config. |
| Cargo | Log bucket, the Datum table for `vm_abuse_*` series, and the `watcher` read user. See [Cargo abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md). |
| Watcher, a Cargo module in each region | Reads Datum and the log store, matches feeds, and sends findings to Central. See [Cargo Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md). |
| Central | All decisions: profiles, cases and policy. See [Central findings from Watcher ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher). |

```text
 Metal host                                          Cargo                                             Central
 counters ─────── vm_abuse_* metrics, every 30 s ──► Datum per-VM table ──┐
 NAT and SNI log ─┬── objects, every 5 min ────────► log bucket ──────────┼──► Watcher ── findings ──► cases, policy
 proxy access log ┘                         blocklists, abuse databases ──┘                             │
                                                                                                        │ restriction API
 Metal host            host sync (10 s)      Atlas app                                                  │ (central_only)
 metal_abuse chain ◄──── VM config ───────── VM Restriction ◄───────────────────────────────────────────┘
                   ───── restriction_status ►               ── vm.restriction ──► Central
```

## Pages

| Page | Content |
| --- | --- |
| [Metrics](metrics.md) | Per-VM counters, port classes, SNI log, NAT log, baseline script |
| [Enforcement](enforcement.md) | `metal_abuse` chain, restriction kinds, restriction states, HTTP proxy, inbound DDoS |
| [Contract](contract.md) | Metal and Atlas, restriction API, events to Central, telemetry, log upload |
| [Data](data.md) | Records, retention, time source |
| [Layouts](layouts.md) | DocType mockups. They wait for engineer approval. |

Cargo pages:

- [Watcher ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md): inputs, finding codes, thresholds, feeds, findings, attribution, calibration.
- [Abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md): log bucket, keys, per-VM metrics table.

Central pages:

- [Abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md): findings from Watcher, profiles, cases, ladder, policy, lifts, reports, customer messages, deploy order.
- [CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md): reporting tiers, clock and workflow.
- [Abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md): Central DocType mockups.
