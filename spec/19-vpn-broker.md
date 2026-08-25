# The customer gateway (WireGuard dial-in) — superseded, retired

> **Retired.** Atlas still brokers **WireGuard** dial-in for a VM's owner, but the
> host-terminated broker this chapter used to describe — one `wg-<id>` interface
> **per tunnel**, terminated on the **host** in the root network namespace — is
> gone. It is replaced by the **customer gateway**: a single shared `wg0` on a
> **gateway VM on the private mesh**
> ([25 → The customer gateway](./25-private-networking.md#the-customer-gateway--external-dial-in-to-the-mesh),
> [26-customer-gateway-desk.md](./26-customer-gateway-desk.md)), where every
> customer is one `[Peer]` and the client lands as a `/128` in its own tenant
> `/48` — so one tunnel reaches **all** the owner's VMs. The live surface is
> `request_vpc_access` / `revoke_peer` (`services/customer_gateway.py`) and the
> `VPN Peer` DocType; the host-terminated broker's `VPN Tunnel` DocType,
> `api/tunnel.py`, the `vm-tunnel` host verb and the per-server tunnel allocators
> were removed with the retirement.
>
> **Why the change.** Host-termination put a public UDP listener on *every* host
> carrying a customer VM, gave each tunnel its own interface (~100 k at fleet
> scale), and steered the decrypted packet with head-inserted nft `drop` rules —
> against Atlas's invariant that **the internet touches VMs, never hosts** (the
> reverse/TCP proxies obey the same rule). The gateway-on-mesh model keeps the
> internet-facing surface to a handful of static-IP VMs, carries all customers as
> peers on one interface, and confines each to its own `/48` with **zero
> per-customer isolation state** — source pinned by WireGuard cryptokey routing,
> destination by one static `same_48` eBPF guard (host-verified program in
> `llm/references/customer-vpc-vpn.md`).

The rest of this chapter is retained only for the **design rationale** that carried
over to the gateway. Read [25](./25-private-networking.md) for what is built. (If a
tenant ever needs to reach exactly *one* VM rather than the whole `/48`, that is a
narrowing of a gateway peer's `AllowedIPs`, not a reason to bring the host-terminated
listener back.)

## Why a tunnel at all (carried over)

A VM's identity is its public IPv6 ([06-networking.md](./06-networking.md)) — but:

- **A client may not have IPv6.** The tunnel's **outer transport is a static public
  IPv4**; the **inner** traffic it carries is v6, so a v4-only laptop or CI runner
  reaches a v6-only VM. In the built design the outer v4 is the **gateway VM's**
  reserved v4; the retired broker used the host's.
- **Private access ≠ public exposure.** The owner reaches every port their VM
  serves — including ports the per-VM public firewall
  ([20-firewall.md](./20-firewall.md)) keeps off the public internet — without
  opening them to the world. On the mesh this is inherent: VPC traffic rides the
  private `fdaa::` plane, which the public firewall (scoped to the host uplink)
  never sees.
- **Scoped to the owner.** The retired broker reached exactly one VM's `/128`; the
  gateway lands the client in its own tenant `/48`, reaching all the owner's VMs and
  nothing else.

## Why it terminates on Atlas-owned infra, never in the tenant's guest (carried over)

Terminating WireGuard inside the *tenant's* guest would make isolation structurally
free, but Atlas rejects it for the same reason it rejects configuring a Reserved IP
inside the guest ([06-networking.md](./06-networking.md)): it breaks the
provider-agnostic, Atlas-owns-nothing-in-the-tenant-guest contract, and in-guest
config is lost on a rebuild / restore / clone that rewrites the guest disk. The
tunnel terminates on Atlas-owned infra — a gateway VM in the built design — so
isolation and revocation are Atlas's to enforce and durable across a guest rebuild.
(A feature that genuinely wants *a tenant VM to join an external WireGuard mesh as a
peer* is a different, guest-terminated feature — not this dial-in.)
