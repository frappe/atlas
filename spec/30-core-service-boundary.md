# The core ↔ service boundary

> **Status: the separate-`satellite`-app design this chapter used to describe was
> ABANDONED.** The core ↔ service *separation* it argued for shipped — but
> **in-app**, as a Python-module boundary, not as a federated second deployment.
> This chapter is kept as a short epitaph so the history is legible and inbound
> links resolve.

## What this chapter proposed (and why it was dropped)

Atlas had accreted service/domain logic — reverse proxy, the customer gateway, the
WireGuard mesh, bench/site deploy, TLS/DNS, subdomain/TCP routing — into the app,
and the generic `Virtual Machine` controller grew to know about it (role fields, a
service-aware `terminate()`, proxy/gateway methods). The proposal here was to split
that logic into a **separate `satellite` app**: its own bench, database, and SSH
engine, joined to Atlas only by a read API, signed lifecycle webhooks, and injected
SSH keys — so *one satellite could federate many Atlas provisioners*.

Only a walking skeleton (one service, the mesh) was ever built. The
federated-deployment answer was then dropped: the network boundary, the second SSH
engine, and the webhook mirror bought complexity a single-region control plane did
not need, and **Central** ([16-central.md](./16-central.md)) already owns the
multi-region, multi-tenant framing a "federation" would have justified.

## What shipped instead — the in-app boundary

The same one-directional separation now lives **inside the one Atlas app** as two
Python subpackages:

- **`atlas/atlas/core/`** — the VM fabric: provision / start / stop / terminate, the
  Firecracker host, tasks & SSH, base + private networking (incl.
  [ANCP](./31-ancp-network-control-plane.md)), placement, migration, snapshots,
  image promotion, and the [Boat](./33-boat.md) seam.
- **`atlas/atlas/services/`** — the PaaS logic: the reverse proxy / TCP proxy /
  front door, subdomain & custom-domain routing, TLS/DNS, bench/site deploy, the
  merged bench admin console, and the customer gateway.

**`services` imports `core`; `core` never imports `services`** — enforced in CI by
the readability lint gate (`.github/scripts/lint_gate.py`), which fails on any
`core → services` import. Core stays **PaaS-blind**: it invokes service logic *by
name* through a callback registry (`core/callbacks.py`), discovering the services
register module via the `services_callbacks` hook (`frappe.get_hooks`), so no core
module names a `services` symbol. The generic VM controller's old service-role
fan-out (`terminate()` teardown, `deploy_gateway`, `read_proxy_maps`, the
address-changed reroute) is inverted onto that registry.

## Residue

`atlas/atlas/api/satellite.py`, `satellite_events.py`, and `bench_routing.py` are
**deleted**. The one mechanism worth keeping — injecting the control plane's own SSH
public keys into every guest — survives under neutral names:
`Atlas Settings.satellite_public_keys` → `service_public_keys`, and
`satellite_routing_base_url` → `guest_routing_base_url` (the in-guest routing base
URL that is now a Boat contract).
