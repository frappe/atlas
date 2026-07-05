# The customer gateway — Desk-facing changes

> **Scope.** This chapter is the **Desk / DocType surface** for the customer
> gateway (spec/25 [Phase 5](./25-private-networking.md#the-customer-gateway--external-dial-in-to-the-mesh)):
> the `VPN Peer` DocType, the `is_gateway` VM role, the controller
> methods, and the operator/customer-facing form UX. The **network design**
> (topology, isolation proof, the `same_48` eBPF program, the address plan) is
> settled in [spec/25](./25-private-networking.md) and
> `llm/references/customer-vpc-vpn.md` — read those for *why*; this states *what
> the Desk shows and what a row does*.

The one-line pitch, from the customer's chair: **generate a keypair, paste the
public half into Atlas, get back a `.conf`, `wg-quick up` — and every VM in your
VPC is reachable at its stable `fdaa:` address from your laptop, over one
tunnel, from any IPv4 network.** The gateway automatically enrolls the laptop
the moment the row is created, so the customer's next `wg-quick up` just works.

## v1 shape (this iteration)

Per the operator's steer, v1 stands up a **separate gateway VM with its own
reserved IPv4** — the proxy VM's sibling, not (yet) folded into the proxy. The
merge-into-proxy is a later collapse and needs no schema change: the gateway is
already an independent infra VM role with its own reconcile, exactly like the
proxy is today.

Resolved from the design's open questions (spec/25 §13 / reference §13):

- **One gateway VM per region.** A single-region deployment (region = 0, the
  default) runs exactly one gateway VM. A second gateway is a pure horizontal
  shard past a peer-count ceiling — no design change.
- **Bidirectional.** A tenant's VMs can originate back to the laptop (health
  checks, "push a build to my laptop"), so the client `/128` is advertised into
  the host mesh. This is the one mesh delta the feature adds.
- **No infra `/48` reach, no site-to-site, no standby gateway, no key
  rotation** in v1 — all deferred (spec/25 §13 #4–#6).

---

## 1. `VPN Peer` — the DocType

One row per customer **device** (a laptop, a CI runner). Deliberately **not**
[spec/19](./19-vpn-broker.md)'s `VPN Tunnel`: there is no per-tunnel interface,
port, slot, or per-row server key — the gateway has **one** `wg0`, on **one**
port, with **one** public key, shared by every peer. That deletion *is* the fix
(reference §8).

| Field | Type | Notes |
| --- | --- | --- |
| `tenant` | Link → Tenant | **immutable after insert** — the scope. The client lands inside *this* tenant's `/48`. |
| `gateway` | Link → Virtual Machine | the region gateway VM that terminates it (denorm target; filtered to `is_gateway=1`). Resolved automatically on insert; the operator does not pick it. |
| `label` | Data | operator/customer-facing name — `alice-laptop`, `ci-runner`. Required. |
| `status` | Select | `Pending` → `Active` → `Revoked`. Read-only (driven by the controller). |
| `client_public_key` | Data | the customer's WireGuard **public** key (immutable). **No private key is ever stored or transmitted.** Validated by `wireguard.is_valid_public_key` before insert. |
| `client_address` | Data (RO, computed) | `derive_client_address(tenant, name)` → `fdaa:T:T:1:C…/128`. The `/128` inside the tenant `/48`, 4th hextet `0x0001`. |
| `allowed_ips` | Data (RO, computed) | `derive_tenant_prefix(tenant)` → the tenant `/48`. The **customer-side** routing (advisory). |
| `endpoint` | Data (RO, computed) | `<gateway reserved v4>:51820`. What the customer dials. |
| `server_public_key` | Data (RO) | the gateway's `wg0` public key, **read from the gateway** at enroll time and denormed here for the config dialog. One key per gateway, shared by all peers — it is *not* per-row identity, just a legibility denorm so the form can render the `.conf` without re-SSHing. |

**Naming:** `hash` (like `VPN Tunnel`) — the row name is the seed for
`derive_client_address`, so it must be a stable UUID, which `hash` autoname
gives. Computed fields are filled in `before_validate`, the same denorm
discipline as `Virtual Machine.private_address`.

**Permissions:** owner-scoped read like `VPN Tunnel` — a Tenant's owner sees
their own peers; the service user (Central) can create/read across tenants
([spec/16](./16-central.md)). `tenant` is an *attribution/scope* field, not a
Frappe permission role (the spec/25 non-goal holds).

---

## 2. The `is_gateway` VM role

A new operator-owned infra VM flag on `Virtual Machine`, modelled exactly on
`is_proxy`:

| Field | Type | Notes |
| --- | --- | --- |
| `is_gateway` | Check (default 0) | marks this VM as a region customer-gateway. Mutually exclusive with `is_proxy` at validate. |

A gateway VM is otherwise an ordinary **infra mesh guest** (it has an infra
`fdaa:` address in the reserved infra `/48`, like the proxy's tap) with two
extra properties:

- a **fixed reserved public IPv4** (a `Reserved IP` attached, the proxy story —
  so the customer's `Endpoint` never moves across a rebuild);
- a **baked `wg0`** carrying the static `same_48` eBPF guard + the `iifname wg0
  drop` input rule (attached once at bake, never per customer — reference §9).

`reconcile_gateway(gateway_vm)` syncs the desired peer set → live `wg0` over
**guest-SSH** (the proxy idiom, *not* the host-SSH mesh path — the gateway is a
guest). Convergent + idempotent like `reconcile_proxy`: read `wg show wg0 dump`,
`wg syncconf` on drift.

---

## 3. Controller methods (audited as Tasks)

Module-level whitelisted functions (owner-scoped + Central-callable as the
service user, the [spec/16](./16-central.md) pattern), each dispatched as a Task
so it is audited and retryable:

### `request_vpc_access(tenant, client_public_key, label)`

The single entry point a customer's action funnels through.

1. Validate `client_public_key` with `wireguard.is_valid_public_key` — a
   malformed key fails **in the controller**, never on the host.
2. Resolve the region's `gateway` VM (the one `is_gateway=1` VM; error clearly
   if none exists — "no gateway provisioned for this region").
3. Insert the `VPN Peer` row (`Pending`); `before_validate` computes
   `client_address`, `allowed_ips`, `endpoint`.
4. `reconcile_gateway(gateway)` — renders the **full** desired `[Peer]` set from
   all non-`Revoked` peers for that gateway and `wg syncconf`s it onto `wg0`,
   adding this peer with `AllowedIPs = <client>/128` (the source pin). Reads the
   gateway's `wg0` public key back and denorms `server_public_key`.
5. Push the client `/128` into the host mesh (the one mesh delta —
   `reconcile_host_mesh` learns the `/128` on the gateway VM's host, so VMs can
   reach the laptop back).
6. Set `Active`, return the copy-paste config + setup instructions.

### `revoke()`

1. `reconcile_gateway(gateway)` with this row now `Revoked` — drops the peer
   from `wg0`.
2. **Withdraw** the client `/128` from the host mesh (the exact teardown-bug
   class spec/25 flags for `/128`s — reconcile on teardown, not only on enroll).
3. Set `Revoked`.

Losing the last VM does **not** auto-revoke — a customer may hold a tunnel to an
empty VPC and provision into it. Revocation is explicit or cascades from tenant
deletion.

### `reconcile_gateway(gateway)` (idempotent backstop)

Convergent read-diff-push, callable from a scheduler sweep so a rebuilt gateway
self-heals: read live `wg show wg0 dump`, compute the desired peer set from the
rows, `wg syncconf` on drift. A single `request`/`revoke` is the hot-path delta;
the full sweep is the backstop, the same split as `reconcile_host_mesh`.

---

## 3a. Data plane — what `deploy_gateway` + `reconcile_gateway` wire (host-proven)

The gateway VM is a **transit router** on the mesh, not a tenant endpoint, so it needs
plumbing a normal VM never gets. All of it is reconciled from the rows (added on enroll,
withdrawn on revoke) and was proven end-to-end on real DO hosts (a real `wg-quick` client
reached its same-tenant VM; cross-tenant + gateway-self were dropped).

**On the gateway guest** (`gateway.py`, via `deploy_gateway`):
- `wg0` up, MTU 1420, the gateway's minted key, `ListenPort 51820`;
- the WireGuard kernel module (`modprobe`, or `linux-modules-extra` on a generic image —
  a purpose-baked gateway image ships it in-kernel);
- the static `same_48` eBPF guard on `wg0` tc ingress + the `iifname wg0` input drop;
- **IPv6 forwarding on** + `fdaa::/16 via fe80::1 dev eth0` (the gateway routes the private
  plane at its host, like any mesh guest);
- per-client `<client>/128 dev wg0` (so a client-destined reply enters wg0 to be encrypted
  — `wg set` adds no `AllowedIPs` route, unlike `wg-quick`).

**On the gateway's host** (`reconcile_gateway` over host-SSH):
- two `inet atlas forward` accepts for the gateway veth — `iifname <gw-veth> daddr
  fdaa::/16 accept` (client→VM transit; the eBPF guard already confined the tenant) and
  `iifname wg-mesh oifname <gw-veth> daddr fdaa::/16 accept` (VM→client return);
- the client `/128` return route in **two** namespaces: root `<client>/128 via fe80::3 dev
  <gw-veth>` and, inside the gateway netns, `<client>/128 via <guest-link-local> dev <tap>`
  — the `via <guest-link-local>` is load-bearing (the client `/128` is a *forwarded*
  address the guest does not own, so a bare `dev <tap>` has no ND neighbor and loops). The
  guest link-local is EUI-64-derived from the VM MAC (`derive_guest_link_local`).

The `wg syncconf` peer push re-asserts the listen port + key **after** syncconf (syncconf
rewrites the whole `[Interface]` from the peer-only file and would otherwise clear them —
the same trap the host mesh documents).

## 4. Form UX (`vpn_peer.js`)

Modelled on `vpn_tunnel.js`, state-gated buttons + an intro that walks the
customer through the keypair step. **Key difference from `VPN Tunnel`: there is
no manual "Bring up" step** — enrollment is automatic on insert
(`request_vpc_access` reconciles the gateway before returning), so a freshly
saved row is already `Active` and the customer's `wg-quick up` just works. The
form is mostly a **config-delivery surface**.

**New (`is_new`) intro:**

> On your machine run `wg genkey | tee privatekey | wg pubkey > publickey`, paste
> the **public** key below, pick your Tenant, give the device a label, and Save.
> Atlas enrolls your laptop on the gateway automatically — then **Show client
> config** gives you the `.conf`. Your private key never leaves your machine.

**State-gated buttons:**

| Status | Buttons |
| --- | --- |
| `Active` | **Show client config** (primary — the copy-paste `.conf` dialog + setup steps), **Re-enroll** (success — re-runs `reconcile_gateway`, e.g. after a gateway rebuild), **Revoke** (danger, destructive-confirm on the `label`) |
| `Revoked` | none — terminal; intro says "create a new peer to reconnect" |
| `Pending` | (transient — a row only sits here if the reconcile failed; **Re-enroll** to retry, **Revoke** to abandon) |

**The config dialog** (`Show client config`) renders the exact `.conf` block
(reference §4), with the two `AllowedIPs` explained inline — the single most
confusing point:

```ini
[Interface]
PrivateKey = <your client private key — paste from your privatekey file>
Address    = fdaa:T:T:1:C…/128            # client_address (your laptop's VPC /128)

[Peer]
PublicKey  = <server_public_key>           # the gateway's one wg0 key
Endpoint   = <gateway-reserved-v4>:51820   # endpoint
AllowedIPs = fdaa:T:T::/48                  # allowed_ips — route your WHOLE VPC out the tunnel
PersistentKeepalive = 25                    # hold the UDP path open through your NAT
```

The dialog's summary line states plainly: *"This tunnel reaches every VM in your
`<tenant>` VPC by its `fdaa:` address, and nothing else — not other tenants, not
the internet. Replace `<your client private key>` with your `privatekey`
contents; Atlas never sees it."*

**Form links.** `VPN Peer` links off the **Tenant** form ("VPC access")
and the Atlas workspace, alongside `VPN Tunnel`. The gateway VM shows its peers
via a linked-list on the `Virtual Machine` dashboard when `is_gateway=1`.

---

## 5. What the customer does end to end

```sh
# 1. Generate a keypair (private key never leaves this machine).
wg genkey | tee privatekey | wg pubkey > publickey

# 2. In Atlas (or via Central): New VPN Peer, paste the PUBLIC key,
#    pick the Tenant, label it "my-laptop", Save. The row comes back Active —
#    the gateway already knows this peer.

# 3. Show client config → copy the .conf → save as /etc/wireguard/tenant-vpc.conf,
#    paste your privatekey into PrivateKey.

# 4. Bring it up. Every VM in the VPC is now reachable.
wg-quick up tenant-vpc
ssh root@fdaa:T:T:0:V…            # any VM in my VPC, by its fdaa: address
ping6 fdaa:T:T:0:V…               # another VM, same VPC — all reachable
```

Because enrollment happened at Save, step 4 needs no Atlas round-trip — the
gateway accepts the handshake immediately. If the customer later edits their
side's `AllowedIPs` to `::/0`, nothing changes on the security side: the gateway
still accepts only their own `/128` as a source and the `same_48` guard still
drops any cross-tenant destination (reference §4, §6).

---

## 6. Non-goals for the Desk surface (v1)

- **No self-serve customer UI in Atlas Desk** — Central drives
  `request_vpc_access` on the customer's behalf, like every customer action
  ([spec/16](./16-central.md)). The Atlas form is the **operator's** view.
- **No per-peer bandwidth/connection accounting** in the row.
- **No gateway-provisioning wizard** — a gateway VM is stood up by the operator
  like the proxy (set `is_gateway`, attach a reserved IP, bake the guard); this
  chapter assumes it exists.
- **No key rotation button** — re-issue = revoke + new peer.
