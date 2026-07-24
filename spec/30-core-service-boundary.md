# The core ↔ service boundary and the `satellite` app

Atlas opens by declaring itself **the lowest layer** — "No sites, benches, apps,
databases, or workloads" ([README](./README.md)). In practice the app accreted
service/domain logic — proxy, customer gateway, bench/site deploy, pilot,
subdomain/custom-domain/TCP routing, TLS/DNS, and the private-plane **networking
overlays** (host mesh, VPN tunnels) — and the generic `Virtual Machine` controller
itself grew to know about them (role fields, a service-aware `terminate()`,
proxy/gateway methods).

This chapter draws the line as a **provisioner / orchestrator split across a network
boundary**:

- **Atlas is a pure provisioner.** It knows only "a VM exists": provision, start/stop,
  terminate, base networking, the firecracker host, SSH keys, `Firewall`. It hands over
  a **bare Ubuntu VM** and knows *nothing* about services — no role fields, no service
  logic, no in-process service hooks, no execution API.
- **`satellite` is a separate deployment** — its own bench, database, and **SSH engine**
  — that manages every service (mesh, gateway, routing, proxy, bench/site, TLS) for the
  VMs one or more Atlas provisioners hand it. **One `satellite` federates many Atlasses.**

The two are joined by three thin, explicit surfaces (§3), never by an import:

1. Atlas exposes a **read API** (`atlas/atlas/api/satellite.py`) so satellite can mirror
   the VMs/Servers it manages.
2. Atlas emits **signed lifecycle webhooks** (`atlas/atlas/satellite_events.py`) so
   satellite learns when a VM comes/changes/goes.
3. Atlas **injects satellite's SSH public key** into every host (at bootstrap) and guest
   (at provision), so satellite reaches the box it is handed over its *own* SSH.

Satellite holds the *decision* and does the *execution itself*: it SSHes the host for
host-plane work (mesh, gateway) and the guest for guest-plane work (proxy, bench/site,
deploy). Atlas never calls into satellite; satellite never imports Atlas. On a bare
Atlas with no satellite configured, all three surfaces are inert and the VM lifecycle is
exactly a provisioner's.

This reconciles the "lowest layer" non-goal with chapters
[06](./06-networking.md), [12](./12-proxy.md), [17](./17-tcp-proxy.md),
[18](./18-bench-self-routing.md), [19](./19-vpn-broker.md), [25](./25-private-networking.md),
and [26](./27-docker-compat.md): those describe **`satellite`**, not core Atlas.

---

## 1. Inventory — the service-specific parts (what leaves core)

### 1a. Entanglement *inside* the generic VM controller
`atlas/atlas/doctype/virtual_machine/virtual_machine.py`:

- **Service-role fields**: `is_proxy`, `is_gateway`, `build_mode`, `pilot_credential_id`.
- **`terminate()` fan-out**: `_deprovision_proxy()`, `_revoke_tunnels()`,
  `_revoke_vpc_peers()`, `_delete_subdomains()`, `_delete_custom_domains()`.
- **Overlay-networking calls**: `set_private_address()`. (The `_reconcile_host_mesh()` call
  was deleted when ANCP replaced the controller-driven mesh.)
- **Service methods**: `validate_infra_role()`, `set_build_mode_default()`,
  `deploy_gateway()`, `read_proxy_maps()`.

These are removed **incrementally, one service at a time** (§4): a role field leaves in
the phase that moves its owning service out, so every commit keeps a green build (many
core modules still read `is_proxy`/`is_gateway`/`build_mode` until their phase lands).

### 1b. Service modules → `satellite`
`proxy.py`, `tcp_proxy.py`, `customer_gateway.py`, `bench_routing.py`, `bench_image.py`,
`deploy_site.py`, `front_door.py`, `image_recipes.py`, `tls/` + `dns/` registries, the
service half of `api/` (`api/site.py`). (`host_mesh.py` was already deleted — ANCP
replaced it in the core layer before the satellite extraction.) When they move they keep
their **orchestration** but **rebind their transport**: every `run_task`/SSH call becomes
a call to satellite's *own* SSH engine (`satellite/ssh.py`). Atlas keeps its SSH/Task
engine only for its own provisioning — it is no longer a shared executor.

### 1c. Service doctypes → `satellite`
`pilot`, `site`, `site_request`, `subdomain`, `subdomain_denylist`, `custom_domain`,
`port_mapping`, `root_domain`, `tls_certificate`, `tls_provider`, `lets_encrypt_settings`,
`route53_settings`, `powerdns_settings`, `domain_provider`, `vpn_peer`, `vpn_tunnel`, `bench_routing_audit`.
Every satellite doctype is authored via **bench** (`frappe.get_doc({"doctype":"DocType",…})`
/ the Desk editor), committed as the generated `*.json` + controller — never hand-written.
Because these names (`Virtual Machine`, `Server`, …) collide with Atlas's, a satellite
site **never has Atlas installed** — the separate deployment makes the boundary physical.

---

## 2. Atlas — the provisioner surface

Additive, empty-safe, and the whole of what Atlas gains:

- **`atlas/atlas/api/satellite.py`** — token-authed (`only_for("System Manager")`, like the
  Central inbound API) read methods `get_virtual_machine`, `list_virtual_machines`,
  `get_server`. Each returns identity + tenant + the two SSH targets (host IPv4, guest
  IPv6) and **nothing service-specific**.
- **`atlas/atlas/satellite_events.py`** — `doc_events` observers on the VM lifecycle POST a
  thin, **HMAC-signed** webhook (`{atlas, event, virtual_machine, occurred_at}`) to the
  configured satellite. Best-effort, after commit; satellite's reconcile sweep is the
  backstop. Parallel to Central reporting — core stays oblivious to what satellite does.
- **SSH key injection** — `Atlas Settings.satellite_public_keys` is folded into the guest's
  `authorized_keys` at provision (`VirtualMachine._guest_authorized_keys`) and appended to
  the host's at bootstrap (`Server._authorize_satellite_keys`), idempotently.
- **Config** — `Atlas Settings`: `satellite_public_keys`, `satellite_webhook_url`,
  `satellite_webhook_secret`.

Atlas exposes **no** execution API and registers **no** in-Atlas service hooks.

## 3. Satellite — the orchestrator

A standalone Frappe app (no `required_apps`, no Atlas import). Doctypes (§1c plus the
core five):

| DocType | Role |
|---|---|
| `Atlas` | a provisioner satellite federates: `base_url` + read/webhook credentials |
| `Virtual Machine` / `Server` | registration mirrors, scoped by `(atlas, remote_id)` via a `format:` name so registration is a natural upsert; carry the SSH targets |
| `Service` | the handler catalog: `service_key → handler_path` |
| `Service Binding` | a `Service` applied to a VM (the "applies_to" gate is *a binding exists*), with status + per-binding config |

- **`atlas_client.AtlasClient`** reads a provisioner's VMs/Servers over HTTP.
- **`webhook.receive`** (`allow_guest`) verifies the HMAC, resolves the sending Atlas by
  `base_url`, and enqueues a sync (`403` on a bad signature/unknown sender).
- **`registration`** upserts the mirror keyed by `(atlas, remote_id)`; `reconcile()` sweeps.
- **`ssh.run_host` / `ssh.run_guest`** — satellite's own SSH to the host (IPv4) and guest
  (IPv6) with its private key.
- **Service handlers** (`services/*.py`) implement `apply(vm, binding)` / `withdraw(vm,
  binding)`; `Service Binding.after_insert`/`on_trash` dispatch them via `handler_for()`.

**Adding a service** = add a `Service` row pointing `handler_path` at such a class (and,
if durable, a line in `setup.ensure_default_services`), then bind it to VMs. No Atlas
change, ever.

## 4. Phased extraction

- **Phase 0 — walking skeleton.** The whole split proven end-to-end with one service
  (mesh), multi-Atlas-ready. **Done + e2e-verified on a real DO host.**
- **Phase 1** — mesh + gateway/VPN (host-plane overlay): real WireGuard reconcile +
  `VPN Tunnel/Peer` in satellite; delete `customer_gateway`/`is_gateway`.
  (`host_mesh.py` was already deleted — ANCP replaced it in the core layer.)
- **Phase 2** — routing: `Subdomain`, `Custom Domain`, `Port Mapping`, bench-routing.
- **Phase 3** — proxy: fleet + DNS wildcard + cert push; delete `proxy.py`/`is_proxy`.
- **Phase 4** — bench/site/pilot: `Site`, `Site Request`, `Pilot`, `Image Build`,
  recipes; delete `build_mode`.
- **Phase 5** — TLS/DNS/domain: `tls/`, `dns/`, `root_domain`, `tls_certificate`, LE/Route53/PowerDNS.

Each phase relocates the doctype(s), reimplements the logic on satellite's SSH, **deletes
from Atlas core**, verifies, and stops. Central ([21](./21-tunnel.md)) is a separate,
already-decoupled track; `Firewall` stays core for now.

## 5. Status (as landed)

Phase 0 is in the tree and proven end-to-end on a real DigitalOcean host: Atlas's read API
(HTTP + token), the signed webhook receiver, SSH-key injection, and the satellite
orchestrator (five doctypes, HTTP registration, its own SSH engine, and `MeshService`
writing a host's `/etc/satellite/mesh/peers` over satellite's own SSH). Dev topology is
two sites on one bench over HTTP: `atlas.localhost` (provisioner) and a satellite-only
`orchestrator.localhost`. Phases 1–5 move the real service modules/doctypes across the
boundary that is now in place.
