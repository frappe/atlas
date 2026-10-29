# Abuse enforcement

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Atlas restricts traffic at three levels:

| Layer | Owner | Set by | Ends |
| --- | --- | --- | --- |
| [Circuit breaker](#circuit-breakers) | Metal state | Metal, on a threshold breach | Expiry or release |
| [Default block](#default-blocks) | Host rule on every VM | Metal | Never. `is_port_25_allowed` skips the port 25 block. |
| [VM Restriction](#restriction-kinds) | Atlas record | Central, an operator or the customer | Lift or `expires_at` |

Central maps these layers to its ladder: level 0 is a signal, level 2 is a breaker, and levels 3 to 5 are restriction kinds. Metal never goes above level 2. The ladder stops at `isolate`: no pause and no stop. See the [Central ladder ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## Circuit breakers

Metal compares windowed rates with the thresholds in its host config. See [thresholds](detection.md#thresholds). On a breach, Metal applies a breaker and reports an Abuse Signal with `circuit_breaker` set.

A breaker is local because one VM flood can saturate a WireGuard core in seconds. A round trip through Atlas and Central takes minutes.

- A breaker blocks or throttles one kind of traffic.
- Third-party harm breakers do not touch inbound traffic or the mesh. Host safety breakers can.
- The first duration is 30 min. A repeat within 24 h doubles it: 1 h, 2 h, 4 h, up to 24 h. The `strict` profile doubles each duration.
- Metal releases a breaker when the rate stays below half the trigger for 10 min and the minimum duration has passed.
- Atlas reports each breaker to Central in `vm.abuse.signal`. Central decides how breakers change the case severity. See the [Central abuse case ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#abuse-case).
- Metal keeps active breakers in the VM record directory, so a metald restart keeps them. Metal enforces the expiry itself.
- A breaker is Metal state, not a VM Restriction. Only Metal changes it.
- A threshold in Shadow mode writes a signal and applies no breaker.

## metal_abuse chain

`metal_abuse` is an nft chain that the operator owns. It is in the `filter` table of each VM namespace, and nft evaluates it before the [tenant firewall](../networking/host-networking.md#firewall-rules). `LinuxAllocator.Ensure` renders it on every reconcile pass, so an interface restart or a tenant firewall change cannot remove it.

The chain holds the default blocks and the restriction kinds. Egress throttles are `tc` policers next to the [throughput limits](../networking/host-networking.md#throughput-limits).

### Default blocks

Default blocks are host rules on every VM. They are not VM Restriction rows, not ladder steps and not abuse history.

| Block | Rendering |
| --- | --- |
| `smtp_outbound` | Drop new TCP to port 25 on public destinations. Metal skips it when the VM has `is_port_25_allowed`. |
| `mining_pools` | Drop new flows to the pool address set. No exception. Each drop is a signal. |

Port 25:

- Central accepts an unblock request and sets `is_port_25_allowed` on that VM through the Atlas API.
- Only a VM with a dedicated public IPv4 address can get it. Shared NAT addresses are never unblocked.
- The customer sets reverse DNS on the dedicated address.
- A DNSBL listing or an SNDS spam trap hit on an unblocked address opens a case. Central then clears `is_port_25_allowed`.
- Submission ports 465 and 587 stay open. Metal counts them. See [thresholds](detection.md#thresholds).

Mining:

- The pool feed refreshes the nft set every day. See [feeds](detection.md#feeds).
- Ports 3333 and 4444 are not blocked, because legitimate software uses them.
- The stratum match and pool names in the SNI find the pools that the feed does not list.
- Isolation is enough to stop a miner: a miner with no network earns nothing.
- CPU use alone is never evidence.

### Restriction kinds

Each active VM Restriction row renders one kind:

| Kind | Rendering |
| --- | --- |
| `egress_throttle` | `tc` pps and bps policer on public egress |
| `port_block` | Drop new outbound flows to the listed ports. Central sends port classes or ports. Atlas resolves the classes to ports. See [apply a restriction](contract.md#apply-a-restriction). |
| `quarantine` | Drop new outbound flows, except DNS (53) and HTTPS to the package sources in Abuse Settings: Ubuntu mirrors, PyPI and npm, not GitHub. Inbound traffic from the WireGuard gateway and the mesh stays. |
| `isolate` | Drop all traffic, except the WireGuard gateway, backups to Cargo object storage, and Central to Pilot admin |

- All active restrictions apply together. For the same traffic, the most restrictive one wins.
- A lift of one restriction never lifts another.
- Metal also enforces `expires_at` locally, so a restriction ends when Atlas is down.
- Metal reports `host_applied_at` when it renders a restriction. The gap between `applied_at` and `host_applied_at` shows the delay between desired and observed state.

## Who changes restrictions

| Path | Actor | Record |
| --- | --- | --- |
| Normal | Central, from its policy engine or an operator, through the `central_only` [restriction API](contract.md#atlas-api) | Central writes a Resource Action with action `restrict` and links the case. Atlas sets `applied_by = Central`. |
| Customer | The customer applies `quarantine` from the Security tab in Central. Central calls the restriction API. | `applied_by = Customer`. The customer can lift it. Access through the WireGuard gateway stays. |
| Emergency | An `Atlas Admin` creates a VM Restriction in Atlas Desk | Atlas sets `applied_by = Operator` and `applied_by_user`, and sends `vm.restriction`. Central links or opens a case. |
| Expiry | An Atlas scheduler job ends restrictions at `expires_at` | `status = Lifted`, `lifted_by = Expiry`. Metal also enforces it. |

Status of a VM Restriction:

| Status | Meaning |
| --- | --- |
| `Pending` | Atlas has the restriction. Metal has not rendered it yet. |
| `Applied` | Metal rendered it and reported `host_applied_at`. |
| `Lifted` | The restriction ended. `lifted_by` holds the reason. |
| `Failed` | Metal reported that it cannot render the restriction. |

Atlas sends `vm.restriction` on each status change. See [events to Central](contract.md#events-to-central).

The tenant API cannot write restrictions. Lift rules, such as self-lift and automatic return, are Central rules. See the [Central lifts ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## Tenant API behavior

- Power actions are never blocked.
- Route and firewall changes are accepted, but they cannot widen egress. `PATCH .../network` returns the active restrictions.
- `GET` VM returns the optional `restrictions` list: kind, reason code and expiry.
- Terminate: if the VM has `has_open_abuse_case` or an active restriction, Atlas first snapshots the disk to tenant 0. The customer cannot see or delete the snapshot. Atlas deletes it after 30 days. A legal hold from Central, sent in the abuse hold call, keeps it.

## New servers

While a team has an open case at level 4 or 5 (`quarantine` or `isolate`), every new server of the team starts with the same restriction. Central sends it in the optional `restrictions` field of VM create. Atlas creates the VM Restriction rows with the VM, so the VM starts restricted. The restriction ends with the case, when Central lifts it.

## Inbound DDoS

The customer is the victim. These steps protect the customer. Central shows them as protection, and they do not count as abuse history.

1. Sites behind the HTTP proxy are shielded. Central tells customers with public addresses to use the proxy.
2. A receive policer for each VM in `tc`. Metal lowers it when the host uplink is at risk.
3. SYN cookies and conntrack limits on hosts and proxy nodes.
4. XDP drop on the host uplink for an attacked address. It saves host CPU, not link capacity.
5. IP withdrawal by an operator: detach the address, move it to `Blocked` with a cool-down, and give the customer a new address.
6. A null route on a `/32`, by a ticket to the transit provider.
7. Scrubbing, for example Cloudflare Magic Transit, only if attacks become common. This is a cost decision.

Routed IPv6 gets a meter for each source in `services/ipv6-router/nftables/router.nft`. See [IPv6 router](../networking/ipv6-router.md).

As far as we know, no transit provider gives us a remote triggered blackhole.

## Site tombstone

Phishing and illegal content act on the site, not on the VM. Central calls `proxy.suspend_site(name, reason_code)`. The HTTP proxy then serves the abuse tombstone for the site. See [HTTP proxy contract](contract.md#http-proxy).

- The tombstone is a new route value. Today the route value `"-"` means that the site is being prepared.
- The proxy serves a branded "suspended" page with a link to the abuse page: HTTP 403 for abuse, 451 for a legal hold.
- Only an operator lifts a tombstone.
- When Central isolates a VM, it also tombstones the sites of the VM.
- Atlas checks the site hostnames on the HTTP proxy against the phishing feeds in Abuse Settings: when a hostname is added, and every day after. For a match, Atlas sends `vm.abuse.signal` with `Phishing:Site/Reported`. Cargo checks public bucket hostnames itself. See [Cargo public buckets ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#public-buckets).

::: details Code to change

- [Traffic counters](../../metal/internal/network/traffic/bpf/track_traffic.c) count a SYN-ACK as a SYN today.
- [Network convergence](../../metal/internal/network/linux_allocator.go) renders the namespace `filter` table. The `metal_abuse` chain goes there.
- [Firewall rendering](../../metal/internal/network/firewall.go) renders the tenant firewall after `metal_abuse`.
- [Proxy router](../../services/http-proxy/nginx/lua/http/router.lua) handles the `"-"` route value.
- [Public IP Allocation](../../atlas/metal_server/doctype/public_ip_allocation/public_ip_allocation.py) gets `blocked_until`.

:::
