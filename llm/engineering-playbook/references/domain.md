# Atlas and Pilot domain notes

Settled facts as of October 2026. Check current code before relying on a detail.

## Product

- Atlas is the VM management plane for one region: bare-metal providers (Scaleway, AWS, Generic), host preparation, placement, migration, networking, proxy, storage, snapshots. Its dependencies (proxy, Cargo, object storage, IPv6 router, gateways) also run as VMs.
- One Atlas is one region; one region is one provider; no cross-region networking.
- Central owns teams, tenants, sites, billing and usage sync. Cargo owns usage monitoring and buckets. Pilot manages Frappe benches on a VM.
- Moving to production with staging and a production region deployed. Every change stays backward compatible.
- Planned: faster migration, topology placement (anti-affinity), network volumes, Liquid VM. Not planned: server-side placement queue, two interfaces on AWS hosts.

## Ownership

- Atlas stores request and reservation; Metal stores desired and observed runtime state. No VM state machine in Atlas.
- The Virtual Machine name is the Metal VM ID, so create is idempotent. Draft committed before the Metal call; only a Metal `404` deletes a draft.
- Runtime VM fields are virtual; `frappe.get_all` cannot select them. Use `get_doc` or `Virtual Machine State`.
- Metal mutations store desired state, wake the reconciler, return `202`; generations signal completion. Host sets (peers, image policies, privileged addresses) arrive complete via `POST /v1/sync`.
- Metal records reject unknown fields; schema change and binary ship together.
- Memory snapshots and Firecracker state never leave the host.

## Units and identifiers

- MiB, MiB/s, millicores (100 to 32,000 per VM; 32 vCPU cap), integer cents. Integer fields; unit in the name.
- VM names `vm-<tenant>-<number>`, numbers never released.
- Metal Server names are UUIDs with read-only titles `metal-<region>-<n>`.
- Mesh address `fdaa:<region 16>:<tenant 32>:<padding>:<vm>` in `fdaa::/16`; hosts use `fdab::/16` on `wg0`. Region ID immutable once VMs or proxies exist. Tenant 0 is infrastructure.

## Networking

- Underlay, host WireGuard, then WG Mesh (eBPF, NDP-based, controllerless).
- Privileged tenant-0 VMs (proxy, routers, gateways) reach every tenant and vice versa.
- Gateway VMs receive transparently routed packets, so VMs see real client addresses. The IPv6 router and WireGuard gateway build on this.
- The IPv6 router exists because AWS and Scaleway bind a /80 or /64 to one machine while migrated VMs keep their address. Generic providers route the subnet over VXLAN.
- Public IPs come from pools: provider pools reserve through the provider; static pools generate allocations in batches.
- Hosts accept SSH and Metal API only on `wg0`. Development reaches hosts through an Atlas VM gateway. No `0.0.0.0`.
- mTLS Atlas to Metal and between nodes; ZFS streams over OpenSSL.

## Operations facts

- Warm starts clone a template; in-VM changes are lost on restart. Fix the template on every host.
- Warm resume only on a fresh, unchanged disk clone (2026-09-24 incident).
- A stale `pilot worker-pool` serves old code; restart every worker after Python edits.
- Sites without a timezone stamp install rows in IST and scheduled jobs never run; check `time_zone` and `last_execution`.
- Scheduler tick defaults to 240 seconds; sub-minute cron needs `scheduler_tick_interval`.
- Staging CPUs lack SHA-NI; keep SHA-256, no BLAKE3.
- Fleet: 100 hosts (200 max), placement under 100 ms, about 100 concurrent placements.

## Accepted risks

Do not re-raise as they stand. Raise again when the assumption fails or a change worsens the risk.

| Risk | Assumption | Revisit when |
|---|---|---|
| Proxy control boots with zero credentials (401 everywhere). | Controller pushes credentials right after boot. | A path grants access without a credential, or one credential path hides another. |
| Private throughput limit unenforced on IPv6. | Mesh does not reach the guest network yet. | Mesh reaches the guest, or the UI claims enforcement. |
| IPv6 router bit layout caps VM and tenant numbers. | VM IDs will be recycled per tenant. | IDs near the cap before recycling ships. |
| AWS size prices are 0. | Nothing reads them. | Placement or billing reads them. |
| `atlas0` PostUp runs as root via a timer. | Pilot user already has root SSH. | Pilot user loses root, or the file gains writers. |
| Metal nodes in a region trust each other. | Only Atlas issues regional certificates to real hosts. | Certificates reach non-hosts, or untrusted hosts share a region. |
| Cargo's Atlas token is tenant 0. | Cargo needs privileged image operations. | Cargo gains features that do not need tenant 0. |
| Fixed placement deadline and lock wait; clients retry on `placement_busy`. | Contention shrinks as the fleet grows. | Busy rejections stay high at fleet size. |

Settled, do not re-propose: two network interfaces on AWS hosts (one ENI plus Elastic IPs), the old migration source token fix (mTLS coordination replaced it), tests for guest and ops scripts, Python 3.14 `except A, B:` (from `ruff format`), six-field Frappe cron.

## Repositories and commands

- Monorepo: `atlas/` (Frappe), `metal/` (Go `metald`), `services/http-proxy`, `services/wg-mesh`, `services/ipv6-router`, `services/wg-gateway`, `clients/` (generated), `scripts/atlas-vm`. One root `CLAUDE.md`, symlinked as `CLAUDE.md`/`AGENTS.md` per component.
- `pilot`, not `bench`: `pilot --site test.local run-tests --app atlas`, `pilot migrate`, `pilot --site <dev site> build-metald`.
- Lint: `uv run ruff check atlas` (Atlas); `uv run ruff check admin pilot tests` (Pilot).
- Clients: `scripts/generate-api-clients.sh`; CI fails on drift.
- Pilot (`~/pilot/CLAUDE.md`): `Server`, `Bench`, `Site`, `App` entry points, thin CLI and API, `@property` for no-argument nouns, `get_<thing>()` with arguments.
