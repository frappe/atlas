# The core ↔ service boundary and the `satellite` app

Atlas opens by declaring itself **the lowest layer** — "No sites, benches, apps,
databases, or workloads" ([README](./README.md)). In practice the app accreted
service/domain logic — proxy, customer gateway, bench/site deploy, pilot,
subdomain/custom-domain/TCP routing, TLS/DNS, and the private-plane **networking
overlays** (host mesh, VPN tunnels) — and the generic `Virtual Machine` controller
itself grew to know about them (role fields, a service-aware `terminate()`,
proxy/gateway methods).

This chapter draws the line: **Atlas core only knows "a VM exists"**, and everything
service-specific — including special/overlay networking (tunnels, routing, the
private mesh) — becomes a **separate Frappe app, `satellite`** (`frappe/satellite`),
that attaches to the VM lifecycle through an **explicit service registry**.

`satellite` is a **controller + setup scripts only** — it performs no infrastructure
itself. Every host/guest execution, provider call, reserved-IP, image build, and
snapshot runs through **methods Atlas exposes**: satellite holds the *decision*
(desired routing map, peer set, deploy step) and ships the setup scripts; Atlas holds
the *execution* (SSH, providers, the VM fabric). The seam is therefore
**bidirectional** (§3): Atlas → satellite lifecycle notifications, and satellite →
Atlas infra API.

This reconciles the "lowest layer" non-goal with chapters
[06](./06-networking.md), [12](./12-proxy.md), [17](./17-tcp-proxy.md),
[18](./18-bench-self-routing.md), [19](./19-vpn-broker.md), [25](./25-private-networking.md),
and [26](./27-docker-compat.md): those describe **`satellite`**, not core Atlas.

---

## 1. Inventory — the service-specific parts

### 1a. Entanglement *inside* the generic VM controller
`atlas/atlas/doctype/virtual_machine/virtual_machine.py`:

- **Service-role fields**: `is_proxy`, `is_gateway`, `build_mode`,
  `pilot_credential_id`.
- **`terminate()` fan-out**: `_deprovision_proxy()` (DNS wildcard + `TLS Certificate`),
  `_revoke_tunnels()` (VPN broker), `_revoke_vpc_peers()` (gateway),
  `_delete_subdomains()` + `_clear_subdomain_references()`, `_delete_custom_domains()`.
- **Overlay-networking calls**: `_reconcile_host_mesh()` in `provision()` and
  `terminate()`; `set_private_address()` / `_private_network_variables()`.
- **Service methods**: `validate_infra_role()`, `set_build_mode_default()`,
  `deploy_gateway()`, `read_proxy_maps()`.
- **Provision injection**: `ROUTING_BASE_URL` / `_routing_base_url()`.
- **Snapshot rows carry `build_mode`**.

Good precedent: Central reporting is **not** inline — it is `doc_events` observers in
`hooks.py`. The seam generalizes that into an explicit, ordered, lifecycle-aware
registry, turned around so the service also drives infra back through Atlas.

### 1b. Service modules → `satellite`
`proxy.py`, `tcp_proxy.py`, `customer_gateway.py`, `bench_routing.py`,
`bench_image.py`, `deploy_site.py`, `front_door.py`, `image_recipes.py` (the bench/
proxy recipe catalog — see §2), `tls/` + `dns/` registries, and the service half of
`api/` (`api/site.py`; `provision.py`, `firewall.py`, `server_capacity.py`,
`inventory.py` stay core). **Special networking:** `host_mesh.py` (the cross-host
WireGuard private plane) moves too. When these move they keep their
**orchestration** but not their transport: every `run_task`/SSH call **rebinds** to
Atlas's exposed execution API (§3B). The SSH/Task engine (`ssh.py`, `_ssh/*`),
`providers/*`, `scripts_catalog`, and `script_uploads` stay **core** — they are the
executor Atlas exposes.

### 1c. Service doctypes → `satellite`
`pilot`, `site`, `site_request`, `subdomain`, `subdomain_denylist`, `custom_domain`,
`port_mapping`, `root_domain`, `tls_certificate`, `tls_provider`,
`lets_encrypt_settings`, `route53_settings`, `domain_provider`, `vpn_peer`,
`vpn_tunnel`, `bench_routing_audit`. Every satellite doctype is authored via
**`bench new-doctype` / the Desk DocType editor**, then committed as the generated
`*.json` (+ controller stub) — never hand-written schema.

### 1d. Service scripts / on-VM trees → `satellite`
The `bench/` tree (+ `deploy_site.py`), the `proxy/` tree, `scripts/issue-cert.py`,
and the overlay-networking scripts (`tunnel-up.py`/`tunnel-down.py`/`vm-tunnel.py`,
plus `host-mesh`/WireGuard pieces) that serve routing/tunnels rather than a VM's own
base address.

### 1e. Adjacent concern — Central (out of scope)
`central.py`, `central_report.py`, `central_*` doctypes are control-plane *reporting*,
already `doc_events`-decoupled. A separate, optional boundary.

---

## 2. Target boundary

**Atlas core (VM-only) keeps:** `Server`, `Virtual Machine`, `Virtual Machine
Image`/`Snapshot`/`Image Export`/`Migration`, `Reserved IP`, `Firewall` + `Firewall
Rule` (per-VM ACL — a VM's own boundary), `Task`, `Tenant`, SSH Console/Key,
`Provider` + `Provider Image/Size` + vendor Settings, the compute subset of `Atlas
Settings`; modules `networking.py` (**per-VM base addressing only**), `placement.py`,
`provisioning.py`, `migration.py`, `export.py`, `ssh.py`, `providers/*`,
`setup_catalog.py`, `dashboard.py`, `scripts_catalog.py`, `script_uploads.py`; all
VM-lifecycle + base-networking + migration **scripts**.

**`satellite` (service) takes:** everything in §1b–§1d — the reverse-proxy routing
(HTTP subdomain, TCP port-mapping, custom domain), VPN tunnels / customer gateway,
and the WireGuard **host mesh** private plane.

**Base vs overlay networking — the dividing rule.** Core owns what makes *one* VM
exist and reach the internet on *its own* address (public v6, NAT44 v4 egress,
mac/tap/veth, per-VM firewall). `satellite` owns anything that connects VMs *to each
other* or fronts them as a *service* (mesh, tunnels, routing).

**Gray areas decided here:**
- Private-plane `/128` derivation (`derive_private_address`/`derive_tenant_prefix`) is
  pure math used to stamp a field → **stays core** as a pure helper; the *mesh that
  routes it* (`set_private_address`, `_reconcile_host_mesh`,
  `_private_network_variables`) moves behind the seam as a satellite hook.
- The migration keep-address forwarding tunnel (`collapse_forward`, `tunnel-*`) is a
  VM's cross-host continuity → **stays with core migration**; revisit if it grows
  service coupling.
- The image **build engine** (`image_builder.run_build`) is generic → **core**; the
  **recipe catalog** (`image_recipes.py`) → satellite. The engine takes a recipe from
  a registered source, not `import image_recipes`.

---

## 3. The seam — bidirectional

Two contracts. **3A** (Atlas → satellite) notifies satellite at VM lifecycle points.
**3B** (satellite → Atlas) is how satellite effects any infra — it never does so
itself. Both are **landed** in `atlas/atlas/vm_services.py`.

### 3A. Atlas → satellite: the lifecycle registry

Core defines the contract; `satellite` populates it via the `atlas_vm_services`
Frappe hook, so **core never imports satellite**.

```python
# atlas/atlas/vm_services.py  (core)
class VMService(Protocol):
    name: str
    def applies_to(self, vm) -> bool: ...          # cheap; reads custom fields
    def validate(self, vm) -> None: ...            # insert-time rules/defaults
    def provision_variables(self, vm) -> dict: ...  # extra Task env
    def on_provision(self, vm) -> None: ...         # post-provision side effects
    def on_status_change(self, vm, old, new): ...   # optional lifecycle reactions
    def teardown(self, vm) -> None: ...            # terminate, ordered

def vm_services() -> list[VMService]:
    # built from the `atlas_vm_services` hook (dotted paths, declared order);
    # empty on a bare Atlas — every call site then no-ops.
```

```python
# satellite/hooks.py
atlas_vm_services = ["satellite.services.mesh.MeshService", ...]
```

**Core call sites** (in `virtual_machine.py`; additive and empty-safe — the existing
inline logic stays while services are extracted incrementally):

- `validate()`: run each applicable `service.validate` (will absorb
  `validate_infra_role` + `set_build_mode_default`).
- `_provision_variables()`: merge each `service.provision_variables` (will absorb
  `ROUTING_BASE_URL` + the private-plane vars).
- `provision()`: after the Task + commit, run `service.on_provision` (will absorb
  `_reconcile_host_mesh`).
- `on_update()`: on a **status transition**, run `service.on_status_change`.
- `terminate()`: run `service.teardown` for applicable services, ordered between
  core's generic teardown (detach Reserved IP, delete Snapshots) and the
  **mesh-reconcile-last** invariant. The full extraction pins the ordering contract
  to today's sequence exactly (detach IP → gateway peers → routes → proxy DNS →
  snapshots → mesh reconcile last).

**Custom fields, not core fields.** `is_proxy`/`is_gateway`/`build_mode`/
`pilot_credential_id` become **Custom Fields owned by satellite** (shipped as
fixtures), removed from `virtual_machine.json`. Each service's `applies_to()` reads
its own field. Satellite's first field, `satellite_managed`, ships this way today.
Ripple: the core snapshot path copies custom fields generically so `build_mode` still
rides.

**Cross-app form actions.** `deploy_gateway()` / `read_proxy_maps()` become satellite
`@frappe.whitelist()` functions taking a VM name, surfaced as VM-form buttons via
satellite's `doctype_js` hook — core untouched.

### 3B. Satellite → Atlas: the exposed execution API

**Invariant:** satellite opens no SSH/SCP, calls no cloud provider, mutates no host
or guest. It (a) owns its doctypes + orchestration, (b) ships setup scripts as
payloads, and (c) drives every infra effect through Atlas's stable, whitelisted API.
Atlas exposes (mostly formalizing methods that already exist):

- `run_host_script(server, script, variables)` / `run_guest_script(vm, script,
  variables)` — execute a **registered** satellite script on a host / on a VM's guest,
  returning the Task. Both wrap `run_task`; they ride the SSH/Task engine and the
  `fake_tasks` seam (a Fake-backed host/VM synthesizes the Task with no SSH, so a
  satellite service is testable with no droplet). **Landed.**
- **Script registration** via the `atlas_script_directories` hook: satellite
  contributes its script tree (`satellite/scripts/`) into Atlas's `scripts_catalog`
  search paths, so Atlas's runner stages and runs satellite's verbs (per-Task, like an
  unshipped e2e probe). **Landed.**
- `attach_reserved_ip` / `detach_reserved_ip`, `build_image(recipe)`, `snapshot` /
  `clone` / `rebuild` — already-whitelisted VM / Reserved IP / Image Build / Snapshot
  methods, **called** by satellite, not reimplemented.
- Read-only facts: VM address, proxy/gateway fleet membership, server list.

**Domain SaaS stays satellite-owned:** talking to ACME (Let's Encrypt/ZeroSSL) and
DNS (Route53/Cloudflare) is satellite's own domain layer, not Atlas infra; only
*pushing* an issued cert onto proxy guests goes through `run_guest_script`.

### Coverage table — every §1a entanglement → its seam method

| §1a entanglement | Seam method |
| --- | --- |
| `validate_infra_role`, `set_build_mode_default` | 3A `validate` |
| `ROUTING_BASE_URL`, `_private_network_variables` | 3A `provision_variables` |
| `_reconcile_host_mesh` (provision) | 3A `on_provision` |
| `_reconcile_host_mesh` (terminate), `_deprovision_proxy`, `_revoke_tunnels`, `_revoke_vpc_peers`, `_delete_subdomains`, `_delete_custom_domains` | 3A `teardown` |
| `deploy_gateway`, `read_proxy_maps` | 3B whitelisted fns + `doctype_js` buttons |
| every `run_task`/SSH call in the §1b modules | 3B `run_host_script` / `run_guest_script` |
| `is_proxy`/`is_gateway`/`build_mode`/`pilot_credential_id` | satellite Custom Fields (fixtures) read by `applies_to` |

---

## 4. The `satellite` app — shape, CI, DocType workflow

- **Identity:** `frappe/satellite`, a standard Frappe app under `apps/satellite`,
  `required_apps = ["atlas"]`; hooks into Atlas doctypes cross-app (the registry +
  Custom Fields). It must be **co-installed with atlas on the same site** — the seam
  is an in-process hook, not a network call. Mirrors Atlas's `providers/` / `tls/` /
  `dns/` registry idiom.
- **CI defaults = Atlas's.** The same `pyproject.toml` `[tool.ruff]` (line-length 110,
  tab indent, double quotes, matching `select`/`ignore`) and `[tool.isort]`; the same
  `.pre-commit-config.yaml`; `.github/workflows/{ci,linter}.yml` — `ci.yml`
  co-installs atlas + satellite before running `run-tests --app satellite`, and adds a
  `shellcheck` gate on the setup scripts. New code, so it starts fully ruff-clean.
- **DocTypes via bench, never hand-written.** Every satellite doctype is created and
  edited through `bench new-doctype` / the Desk DocType editor, committed as the
  generated `*.json` (+ controller stub). Custom Fields on `Virtual Machine` are
  authored via **Customize Form** and shipped through the **fixtures** hook — never by
  hand-editing `virtual_machine.json`. Controllers hold behavior only.

---

## 5. Testing strategy — mock the VM, verify the service for real

Full coverage that supports Atlas without paying for cloud droplets, built on Atlas's
existing Fake seam and one new idea: **fake the host, run the guest for real.**
Because satellite drives Atlas's exposed executor (§3B) for *all* infra, the fake
plugs in at exactly one seam — swap the executor, not scattered mocks.

- **Reuse Atlas's Fake seam.** `providers/fake.py` makes a VM *exist* (a row that
  reaches Running) with synthetic, unroutable IPs; `providers/fake_tasks.py` makes
  every host-side Task succeed with no SSH.
- **The faithful double.** Satellite's service logic reconciles *guest* state over
  guest-SSH. Instead of stubbing that, stand up the **real guest service locally**
  (nginx / WireGuard in a container / nspawn / loopback on the CI runner) and point
  the guest-SSH transport at it. The **same test** needs no droplet *and* proves the
  service works.
- **Three tiers:**
  1. **Unit** — pure logic (map generation, peer-set derivation, validation, teardown
     ordering); milliseconds, no host.
  2. **Faithful-double** — Fake host + real local guest service; drives a real
     `Virtual Machine` row (Fake provider) through `provision`/`terminate` and asserts
     the registered satellite hooks fired in the right order with the right effect.
  3. **e2e (optional)** — real droplet for host facts only, grouped by use case.
- **Boundary contract tests** on both sides: Atlas asserts an empty registry is a
  no-op ([`test_vm_services.py`](../atlas/tests/test_vm_services.py)); satellite
  asserts its handlers register and fire at each seam point.

---

## 6. Phased extraction

0. **Land the seam in core, behavior-unchanged** — `vm_services.py` + hook read + call
   sites; default registry empty; empty-safe. **Done.**
1. **Custom-field-ize** `is_proxy`/`is_gateway`/`build_mode`/`pilot_credential_id`.
2. **Scaffold `satellite`** (bench: app + CI + doctype workflow) with the tiered test
   harness and the faithful guest double. **Done** (scaffold + MeshService demo).
3. **Move overlay networking first** — `host_mesh` (MeshService) + gateway/VPN
   tunnels, the cleanest flag-gated seam.
4. **Move routing** — subdomain / custom domain / port mapping / bench_routing.
5. **Move proxy** — proxy fleet + DNS wildcard + cert push.
6. **Move bench/site/pilot** — site, pilot, deploy_site, front_door, bench_image,
   bench recipes + `bench/` tree.
7. **Move TLS/DNS/domain** — `tls/`, `dns/`, root_domain, tls_certificate, vendor
   settings, `issue-cert.py`.
8. **Central** — separate track; already decoupled.

Each phase is independently shippable; a bare Atlas always boots with an empty
registry.

## Status (as landed)

Phase 0 (the seam) and the phase-2 scaffold are in the tree. `atlas/atlas/vm_services.py`
carries the `VMService` protocol, the hook-built registry, and the exposed
`run_host_script` / `run_guest_script`; `virtual_machine.py` calls the registry at all
five lifecycle points, empty-safe. `frappe/satellite` ships `MeshService` (the private
mesh as a VMService demonstrating both seam directions end-to-end), the
`satellite_managed` Custom Field, the `satellite-mesh-*` host scripts, mirrored CI, and
the unit + faithful-double test tiers. The remaining phases (3–8) move the real service
modules/doctypes behind the seam that is now in place.
