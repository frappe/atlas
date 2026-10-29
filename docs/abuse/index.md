# Abuse protection

Status: planned. Atlas does not implement this yet.

## Purpose

Abuse protection finds and limits abuse that starts from, or targets, tenant resources in a region. Atlas finds abuse in the data plane and restricts the traffic. Central owns the customer side: cases, escalation, lifts, abuse reports, customer messages, the operations dashboard and CERT-In reports. See the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

In scope for Atlas:

- Outbound abuse from VMs: floods, scans, brute force, spam, reflectors, command and control (C2) traffic and crypto mining.
- Hosted content on proxy sites: the proxy serves a tombstone page for a suspended site.
- Inbound DDoS on tenants and on the platform.

Out of scope:

- Guest spoofing and traffic between tenants on the mesh. WG Mesh `local_vms` and `can_communicate` already stop them. See [WG Mesh](../networking/wg-mesh/index.md).
- An agent in the guest. Guests are untrusted.

## Principles

1. Atlas finds abuse and restricts traffic. Central decides about the customer. Atlas does not know teams, trust tiers or contacts.
2. Atlas acts alone only to protect the region. It uses circuit breakers that are narrow and short, and that expire without help.
3. Restrictions live in a layer that the tenant API cannot write. A tenant firewall, route or IPv4 change cannot widen them.
4. Count only the traffic that the guest starts. Never count replies. Today `track_traffic.c` counts a SYN-ACK as a SYN.
5. Every restriction has a reason, an actor, a case and an end condition.
6. Restrict traffic, never compute. The abuse system never pauses or stops a VM.
7. Keep evidence before it is lost. Atlas snapshots the disk before it terminates a VM with an open case or an active restriction. Never stop and start a VM to collect evidence. See the [guest disk corruption incident](../incidents/2026-09-24-guest-disk-corruption.md).
8. A detection failure must not block customers. If a detector, Atlas or Central is down, nothing new is applied, and breakers still expire.
9. Atlas stays backward compatible: optional fields, optional events and new eBPF maps only. Central deploys first.

## Who does what

| Component | Does | Acts alone |
| --- | --- | --- |
| Metal, on each host | Counters, SNI log, NAT log, detectors, circuit breakers, the `metal_abuse` nft chain, default blocks | Breakers only |
| Atlas app, in each region | Abuse Signal, VM Restriction, Public IP Assignment, Abuse Settings, feeds, attribution, restriction API, events to Central | Emergency restrictions by an operator only |
| HTTP proxy | Rate limits, abuse tombstone, access log upload | No. It follows Atlas and Central. |
| IPv6 router | Meter for each source, restriction set | No |
| Central | Cases, escalation, lifts, reports, customer messages, CERT-In | Yes |
| Cargo | Regional log bucket. See [Cargo abuse logs ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md). | No |
| Pilot | Nothing | Not applicable |

```text
 Metal host                 host sync (10 s)   Atlas app              webhook       Central
 counters, SNI and NAT log  ───────────────►  Abuse Signal         ────────────►  cases, policy
 breakers, metal_abuse      ◄───────────────  VM Restriction       ◄────────────  messages, reports
                              host config      Abuse Settings        central_only   CERT-In
                                               attribution              API
 HTTP proxy: limits, tombstone          Cargo: log bucket (180 days)
```

## Pages

| Page | Content |
| --- | --- |
| [Detection](detection.md) | Counters, SNI log, findings, thresholds, profiles, overrides, feeds, fleet data, calibration |
| [Enforcement](enforcement.md) | Circuit breakers, `metal_abuse` chain, default blocks, restriction kinds, restriction changes, inbound DDoS, site tombstone |
| [Contract](contract.md) | Metal and Atlas, Atlas API additions, events to Central, HTTP proxy, log upload, deploy order |
| [Data](data.md) | Records, logs, public IP history, retention, metrics |
| [Layouts](layouts.md) | DocType mockups. They wait for engineer approval. |

Central pages:

- [Abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md): cases, ladder, policy engine, lifts, reports, customer messages, operations.
- [CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md): reporting tiers, clock and workflow.
- [Abuse layouts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE_LAYOUTS.md): Central DocType mockups.
