# Customer gateway (WireGuard VPC dial-in) — build progress

Building **spec/25 Phase 5 / VPC-Phase A**: the customer gateway — external dial-in to
a tenant's VPC. A customer runs stock `wg-quick`, dials the region's GATEWAY VM's fixed
public v4 `:51820`, and lands inside their tenant's `/48`, reaching every VM in their VPC
by its stable `fdaa:` address. Design settled in
[spec/25](../spec/25-private-networking.md#the-customer-gateway--external-dial-in-to-the-mesh),
Desk surface drafted in [spec/26](../spec/26-customer-gateway-desk.md), full rationale +
the host-proven eBPF program in [references/customer-vpc-vpn.md](./references/customer-vpc-vpn.md).

Depends only on the host mesh (Phase 1, shipped + proven). This is an on-ramp to that
fabric: it decrypts one more peer on a gateway VM and drops the packet onto the mesh.

## Operator steer (this session)
- Deploy a **separate gateway VM with its own IPv4** now; merge into the proxy later
  (needs no design change — gateway = proxy's sibling infra VM already).
- Any customer can securely connect to their VPC over the gateway after this.
- Draft the Desk-facing DocType/changes doc → `spec/26-customer-gateway-desk.md`.
- Ignore spec/19 (VPN Broker) — this is a clean, different design.

## Design decisions resolved (spec/25 §13 / reference §13)
1. Isolation mechanism = **eBPF `same_48`** (host-proven 2026-07-02; zero per-customer state).
2. **One gateway VM per region** (single-region = one gateway).
3. **Bidirectional** — client `/128` advertised into the mesh so VMs reach the laptop back.
4. No infra-`/48` reach, no site-to-site, no standby gateway, no key rotation (all v1 deferred).

## What shipped (this session, on `main` — a live bench)

### Controller + DocType (host-free, unit-covered)
- **`atlas/atlas/networking.py`** — `derive_client_address(tenant, peer)`: the client `/128`
  inside the tenant `/48`, 4th hextet `0x0001` (disjoint from VMs' `0x0000` by
  construction), low 48 bits HKDF(peer UUID). Host-independent, `client & /48 == tenant
  prefix` (the same_48 identity). + `WG_GATEWAY_PORT`, `CLIENT_HEXTET` constants.
- **`VPN Peer` DocType** (json/py/js) — one row per customer device. `tenant`
  (immutable scope), `gateway` (denorm, auto-resolved), `label`, `status`
  (Pending→Active→Revoked), `client_public_key` (immutable, validated), computed
  `client_address`/`allowed_ips`/`endpoint`, denormed shared `server_public_key`. NO
  per-tunnel interface/port/slot — the deletion IS the fix. Form is a config-delivery
  surface (auto-enroll on Save; Show client config / Re-enroll / Revoke).
- **`Virtual Machine.is_gateway`** (Check) + `validate_infra_role` (mutually exclusive with
  `is_proxy`) + `deploy_gateway`/`_revoke_vpc_peers` methods + Desk "Deploy gateway" button.
- **`atlas/atlas/customer_gateway.py`** — the gateway control plane (proxy sibling):
  `resolve_region_gateway`, `request_vpc_access` (whitelisted, owner+Central), `enroll_peer`,
  `revoke_peer`, `reconcile_gateway` (convergent `wg show wg0 dump` → `wg syncconf` over
  GUEST-SSH), `render_wg0_config` (source-pinned `[Peer]` per Active peer, sorted),
  `deploy_gateway` (stages the atlas pkg + eBPF into the guest, compiles the `.o`, writes
  env + service, `systemctl enable --now`), `client_config_payload`.
- **`atlas/atlas/host_mesh.py`** — `_add_customer_vpc_clients` folds each Active peer's
  client `/128` into its gateway host's AllowedIPs, so the existing converging
  `reconcile_host_mesh` advertises it (enroll) / withdraws it (revoke) — the return path,
  no separate delta path.
- **`tenant_dashboard.py`** — "VPC access" links VPN Peer off the Tenant.

### Host side (runs inside the gateway GUEST)
- **`scripts/bpf/vpc_guard.bpf.c`** — the static `same_48` eBPF guard, VERBATIM from the
  host-proven reference (§6.2). Accept iff saddr/48 == daddr/48; fail-closed on non-fdaa.
  The two gotchas (wg0 is L3 — no ethhdr; section `tc`) baked in.
- **`scripts/lib/atlas/gateway.py`** — `bring_up_gateway`: create wg0, mint the shared key
  once (0600), listen :51820, attach the static guard on wg0 tc ingress, add the
  host-local `iifname wg0 drop` in the guest's own `inet gateway` table. Pure command
  builders (unit-testable) + the one host-touching bring-up. NONE of the guard/drop change
  per customer — enrolling is JUST a wg peer.
- **`scripts/systemd/gateway.service`** — boot-safe re-assert (the host-mesh.service pattern).

### Tests — ALL GREEN on tests.local (no host)
- **14 NEW** `test_customer_gateway.py`: peer denorms + immutability, malformed-key +
  no-gateway rejection, `resolve_region_gateway` (one/none/>1), `render_wg0_config`
  (source pin, Revoked dropped, sorted, empty), the host-mesh client-`/128` fold
  (Active joins / Revoked withdrawn), proxy/gateway role exclusivity.
- **7 NEW** derivation tests (`test_private_networking.py::TestClientAddressDerivation`):
  inside-/48, `0x0001` marker, VM/client never collide, host-independence, masked-/48 ==
  tenant, distinct clients, non-UUID tenant name.
- **9 NEW** host-lib `test_gateway.py`: link/key/guard/drop/table command shapes.
- No regressions: VM 51, VPN Tunnel 9, Server 19, Subdomain 12, networking 28,
  private-networking 33 + wiring 18, host-lib 28. **ruff clean** on all changed files.

## E2E on real DO hosts — 7 real-host bugs found + fixed

`run_smoke` (structural) PASSED, then `run` (full L3 with a real wg-quick client on host2)
drove out the data-plane. Bugs found + fixed on real DO droplets:

1. **Guest-SSH key** — the gateway VM must authorize BOTH the ephemeral (host-probe) AND the
   control-plane key (deploy_gateway uses connection_for_guest). Fixed in the e2e provision.
2. **Recycled /128 host key** — the shared e2e fleet recycles guest /128s; a fresh gateway
   inherits a stale known_hosts key and StrictHostKeyChecking=accept-new hard-fails on a
   CHANGED key. e2e purges the entry (ssh-keygen -R) pre-deploy.
3. **No venv on a guest** — `gateway.service` used `/var/lib/atlas/venv/bin/python` (a HOST
   path); a guest has no Atlas venv. Switched to `/usr/bin/python3` (bring_up_gateway is
   stdlib-only). deploy_gateway also runs bring-up directly (not only via the service) so
   errors surface as a failed Task.
4. **No WireGuard in the guest kernel** — the Firecracker guest kernel lacks wireguard. The
   generic e2e image needs `linux-modules-extra-$(uname -r)` (carries wireguard.ko). Added
   modprobe-or-install + persist. (A purpose-baked gateway image ships it.)
5. **wg0 listen port cleared by syncconf** — `reconcile_gateway`'s `wg syncconf` from a
   peer-only config REWRITES [Interface], clearing the listen port (wg picked a random
   port → clients dialed :51820 and missed). The exact wg-mesh key-vs-syncconf trap. Fixed:
   re-assert `wg set private-key … listen-port 51820` AFTER syncconf (order load-bearing).
6. **Gateway forwarding + fdaa route + host transit rules** — the gateway guest didn't
   forward IPv6 and had no `fdaa::/16 via fe80::1` route; its host had no forward-accept for
   the gateway veth as a transit. Added: `net.ipv6.conf.all.forwarding=1` + route in the
   guest; two `inet atlas forward` accepts on the host (`iifname <gw-veth> daddr fdaa::/16
   accept` and `iifname wg-mesh oifname <gw-veth> …`).
7. **Return-path routing loop (the deep one)** — a VM's reply to a client looped
   (hlim-decrementing, dying as ICMP time-exceeded). Root cause: the client /128 is a
   FORWARDED address the gateway guest doesn't own, so `<client>/128 dev <tap>` in the netns
   had no ND neighbor and bounced. FIX (3 routes, mirroring how a VM's own /128 is wired):
   (a) host root netns `<client>/128 via fe80::3 dev <gw-veth>`; (b) gateway netns
   `<client>/128 via <guest-eth0-link-local> dev <tap>` — the `via` is load-bearing (the
   guest answers ND for its own link-local, then forwards); (c) guest `<client>/128 dev wg0`
   (wg set, unlike wg-quick, adds no AllowedIPs route). The guest link-local is EUI-64-derived
   from the VM MAC (`derive_guest_link_local`, no probing). **PROVEN: `3 packets received,
   0% loss` — client reached its same-tenant VM through the gateway.**

All these host routes/rules are reconciled from the rows in `reconcile_gateway`
(`_reconcile_gateway_host_routes` + `_reconcile_guest_client_routes` +
`_wire_gateway_host_forwarding`), so they add on enroll and withdraw on revoke.

## STATUS: BUILT END TO END + PROVEN ON REAL DO HOSTS.
- **Full L3 e2e GREEN** (`customer_gateway.run`, two DO droplets, a real `wg-quick` client):
  `same-tenant reach ✓, cross-tenant drop ✓, gateway-self drop ✓`. `keep=False` torn down
  clean (peer revoked, gateway + VMs terminated, reserved IPs detached — no billable leak).
- **Unit-green:** 14 gateway wiring + 3 client-addr deriv + 3 guest-link-local + 9 host-lib
  gateway commands; zero regressions across VM (51), private-net (36+18), Server, Subdomain.
  ruff clean on every changed file.
- **Scaleway PROD deploy (no tests run there):** `bench migrate scaleway.local` applied the
  additive `VPN Peer` DocType + `is_gateway` field; the controller loads;
  `reconcile_host_mesh`'s new client-/128 fold is a clean no-op on the live 2-host/32-VM
  fleet (no drift, no disruption); web+worker restarted. No gateway VM provisioned on PROD —
  an operator stands one up (set is_gateway, attach a reserved IP, Deploy gateway) when
  wanted.
- **Follow-ups (not blockers):** bake wireguard-in-kernel + clang/libbpf + the compiled
  `vpc_guard.bpf.o` into a purpose-built gateway IMAGE (deploy currently installs them on a
  generic image); a cross-host client→VM e2e (this run put the client's VMs on the gateway
  host; cross-host reach rides the already-proven wg-mesh but isn't yet asserted in this
  use case); the mesh /128 return-path advertisement is proven, the multi-host fan-out is
  the same mesh delta.
