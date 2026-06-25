# The per-VM public firewall

A VM's identity is its public IPv6 ([06-networking.md](./06-networking.md)), and
by default that address is reachable from the **whole** v6 internet: the
`inet atlas forward` chain is `policy accept` with a broad per-VM accept, so every
port the guest serves is exposed. A **Firewall** restricts that public surface to
a chosen set of ports — "only 443", say — while leaving two paths untouched:

- **the VPN tunnel keeps full access** to the whole VM (every port), and
- **the VM's own outbound connections** keep working (their replies are not new
  public ingress).

It is the public-ingress complement of the VPN broker
([19-vpn-broker.md](./19-vpn-broker.md)): the broker gives the *owner* private,
all-port reach over an encrypted link; the firewall governs what the rest of the
*internet* may reach. Together they make "the owner reaches everything, the world
reaches only what you publish" true. Read [06-networking.md](./06-networking.md)
first — this chapter states only where the firewall differs.

## Why, and why opt-in

While VMs sit on public IPv6 with no inbound filter, "only your VM" is not
actually enforceable by the tunnel alone: anyone can reach any VM's public
address directly, tunnel or not. The firewall is what closes the public surface so
the tunnel's isolation means something.

It is **opt-in, per VM**: a VM with **no Firewall attached stays fully public**
(the current behavior — nothing breaks on deploy, the `Port Mapping` proxy path
and public SSH keep working). Attaching a Firewall flips *that one VM* to
**deny-all-public-except-listed**. An empty rule list is meaningful: a firewall
with no open ports denies *all* public ingress, leaving the VM reachable **only**
over its VPN tunnel. One Firewall per VM — a VM's public surface is a single set
of allowed ports.

## The mechanism

A **separate, higher-priority base chain** on the same `forward` hook, not the
existing `forward` chain — so the firewall logic stays isolated and never fights
the tunnel rules for ordering:

```
chain public_filter {
    type filter hook forward priority filter - 5; policy accept;
    iifname <uplink> ip6 daddr <vm> ct state established,related accept
    iifname <uplink> ip6 daddr <vm> tcp dport <p> accept   # one per allowed rule
    iifname <uplink> ip6 daddr <vm> drop                   # public, not allowed -> DROP
}
```

`priority filter - 5` is lower than `forward`'s `filter` (0), so `public_filter`
is evaluated **first**. Three properties make this exactly right:

- **The VPN bypasses the firewall for free.** Every rule is scoped to
  `iifname <uplink>` (the host's public NIC). Tunnel traffic arrives on a `wg-…`
  interface, never the uplink, so it matches nothing here and falls through to
  `forward`, where the tunnel's own accept/drop ([19-vpn-broker.md](./19-vpn-broker.md))
  govern it. No special case is needed.
- **`drop` is terminal; `accept` is not.** A `drop` in this earlier chain ends the
  packet's life across all chains, so a disallowed public port is unreachable. An
  `accept` only ends *this* chain — allowed traffic proceeds to `forward` and is
  delivered exactly as before, so nothing else changes.
- **`established,related` keeps the VM's outbound alive.** A reply to a connection
  the VM opened arrives from the uplink destined to the VM but is not new ingress;
  the first rule lets it through. This is the one real behavioral addition — it
  turns conntrack on for the forward hook.

Because the chain is stateful, applying a deny blocks **new** connections but does
not tear down flows already in the conntrack table — an open SSH session survives
the rule that forbids *new* SSH, since its packets still match `established,related`
(a new SYN is state `NEW`, hits the `drop`). This is deliberate, and it is what
every stateful cloud firewall does (AWS security groups, GCP and Azure firewall
rules, DigitalOcean Cloud Firewalls); only a *stateless* filter — AWS Network ACLs —
re-evaluates every packet and would cut the live flow. Atlas keeps the stateful
behavior so tightening a VM's rules never drops the operator's own live session.
Forcibly severing an already-open flow is a host-side conntrack eviction
(`conntrack -D --orig-dst <vm-v6> …`), not a firewall verb — intentionally not part
of apply.

Every rule is `daddr`-scoped to one VM, so per-VM blocks are independent and their
order relative to each other does not matter. `apply_firewall` is idempotent: it
deletes this VM's block by handle and re-appends it (established, then one accept
per rule, then drop), the self-healing contract shared with `apply_tunnel` and
`reserved_ip_nat.py`.

## Durability and teardown

Like the reserved-IP NAT and the tunnels, a firewall is **reconstructible from
disk** with no Frappe DB:

- `firewall-apply.py` writes a per-VM `firewall.env` sidecar (VM IPv6 + the
  allowed `proto/port` list) under the VM directory, then applies the nft block.
- `vm-network-up.py` re-applies it at cold boot (step 10), after the VM's `/128`
  route exists — `apply_persisted_firewall`. No sidecar (no firewall attached) is
  a no-op: the VM stays public.
- `vm-network-down.py` **removes the block** on stop/terminate. This matters for
  correctness: the block lives in the host root netns and survives the namespace
  delete, so a future VM that **reuses the IPv6** must not inherit a stale `drop`.
  The sidecar (in the VM dir) is swept by terminate's `rm -rf`; on a plain stop it
  persists and the next start re-applies the block.

## The doctype and the apply path

- **Firewall** (one per VM — a DB `unique` index on `virtual_machine`, so the rule
  holds even under concurrent inserts; the controller's `exists` check is just the
  friendly message over it): `virtual_machine` (immutable), denormalized `server`
  / `tenant`, an `enabled` toggle, a read-only `status` (Active/Disabled), and a
  child table of **Firewall Rule** (`protocol` tcp/udp, `port`). Owner-scoped
  (Atlas User `if_owner`, System Manager full), like `VPN Tunnel`.
- The controller keeps the host in step on save: `on_update` → `firewall-apply.py
  --action apply` with the rules when `enabled`, else `--action clear` (the VM
  reverts to public); `on_trash` clears. A **terminated** VM is skipped — its host
  state is already gone (`vm-network-down` removed it) and its `network.env` no
  longer exists, so no Task is dispatched.
- The rules cross the Task boundary as a repeatable `--rule tcp/443` flag (the
  typed-list input from [04-tasks.md](./04-tasks.md)); an empty list is the
  deny-all-public firewall.

## Interaction with the proxies

The `Port Mapping` reverse/TCP proxy ([12-proxy.md](./12-proxy.md),
[17-tcp-proxy.md](./17-tcp-proxy.md)) dials a VM's **public IPv6** on a target
port. That arrives as public ingress, so a firewalled VM must **allow the proxied
port** for the proxy to reach it. This is intentional: the firewall is the single
place that decides the VM's public surface, and the proxy is just another public
client of it.

## Non-goals / follow-ups

- **IPv4 / reserved-IP ingress filtering.** The first cut scopes to public IPv6
  (every VM's universal public surface). A reserved-IP VM's inbound v4 (DNAT'd to
  its private /30 — [06-networking.md](./06-networking.md)) is not yet filtered.
- **Source scoping and port ranges.** Rules are `proto/port` for any source; CIDR
  sources and port ranges are a later refinement.
- **Inter-VM traffic** stays as today (the forward chain's per-VM accepts); the
  firewall governs the *public uplink*, not VM↔VM.
- **The Desk UI is built** (`firewall.js`): a `Firewall` form with the allowed-port
  table and an **Apply to host** button (the explicit apply verb — a plain Save
  never SSHes), linked off the Virtual Machine dashboard ("Network access") and the
  Atlas workspace. **The SPA surface** still lands with the rest of the user
  dashboard ([11-user-ui.md](./11-user-ui.md)).
