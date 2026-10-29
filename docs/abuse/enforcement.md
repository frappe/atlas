# Abuse enforcement

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Central decides each restriction and calls the Atlas [restriction API](contract.md#restriction-api). Atlas keeps the restriction as a VM Restriction and puts it in the VM config. Metal renders it in the VM namespace. Metal and Atlas never restrict a VM by themselves.

The ladder, lifts and policy are in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## metal_abuse chain

`metal_abuse` is an nft chain that the operator owns. It is in the `filter` table of each VM namespace, and nft evaluates it before the [tenant firewall](../networking/host-networking.md#firewall-rules). `LinuxAllocator.Ensure` renders it on every reconcile pass, so an interface restart or a tenant firewall change cannot remove it.

The chain holds the default block and the restrictions in the VM config. Throttles are `tc` policers next to the [throughput limits](../networking/host-networking.md#throughput-limits).

The chain has one default block, `smtp_outbound`:

- It drops new TCP flows to port 25 on public destinations.
- It applies to every VM. It is not a VM Restriction and not abuse history.
- Metal skips it when the VM has `is_port_25_allowed`. Only Central sets this field, through VM update. See [restriction API](contract.md#restriction-api).
- Each drop adds 1 to `vm_abuse_smtp_blocked_total`. See [counters](metrics.md#counters).
- Submission ports 465 and 587 stay open.

The rules for a port 25 unblock are Central rules.

## Restriction kinds

Each active VM Restriction renders one kind:

| Kind | Parameters | Rendering |
| --- | --- | --- |
| `egress_throttle` | `pps`, `bps` | `tc` policer on public egress |
| `ingress_throttle` | `pps`, `bps` | `tc` policer on traffic to the VM. See [inbound DDoS](#inbound-ddos). |
| `port_block` | `ports`: a list of port and protocol | Drop new outbound flows to the listed ports |
| `quarantine` | None | Drop new outbound flows, except DNS (53) and HTTPS to the package sources: Ubuntu mirrors, PyPI and npm, not GitHub. Inbound traffic from the WireGuard gateway and the mesh stays. |
| `isolate` | None | Drop all traffic, except the WireGuard gateway, backups to Cargo object storage, and Central to Pilot admin |

- `port_block` takes explicit ports only. Central turns a [port class](metrics.md#port-classes) into ports.
- The package sources of `quarantine` are a fixed list in metald.
- All active restrictions apply together. For the same traffic, the most restrictive one wins.
- A lift of one restriction never lifts another.
- Metal also enforces `expires_at` locally, so a restriction ends when Atlas is down.
- Restrictions are VM config, so they move with the VM on migration.
- A new server starts restricted when Central sends restrictions in VM create. Atlas creates the VM Restriction rows with the VM.

The tenant API cannot write restrictions:

- Power actions are never blocked.
- Route and firewall changes are accepted, but they cannot widen egress. `PATCH .../network` returns the active restrictions.
- `GET` VM returns the optional `restrictions` list.

## Restriction states

| Status | Meaning |
| --- | --- |
| `Pending` | Atlas has the restriction. Metal has not rendered it yet. |
| `Applied` | Metal rendered it and reported `host_applied_at`. |
| `Lifted` | The restriction ended. `lifted_by` holds the reason. |
| `Failed` | Metal reported that it cannot render the restriction. |

Metal reports `host_applied_at` or an error in `restriction_status`. See [Metal and Atlas](contract.md#metal-and-atlas). The gap between `applied_at` and `host_applied_at` shows the delay between desired and observed state.

Who changes a restriction:

| Path | Actor | Record |
| --- | --- | --- |
| Normal | Central, through the `central_only` [restriction API](contract.md#restriction-api) | `applied_by = Central`. Central keeps the Resource Action and the case. |
| Emergency | An `Atlas Admin` creates a VM Restriction in Atlas Desk | `applied_by = Operator` and `applied_by_user`. Central links or opens a case from the event. |
| Expiry | An Atlas scheduler job ends restrictions at `expires_at` | `status = Lifted`, `lifted_by = Expiry`. Metal also enforces it. |

Atlas sends `vm.restriction` on each status change. See [events to Central](contract.md#events-to-central).

## HTTP proxy

Static rate limits, for each client address and site:

- `limit_req`: 100 requests a second, with a burst of 500.
- `limit_conn`: 200 connections.

The values are high on purpose, because Indian mobile networks put many users behind one CGNAT address.

Site tombstone. Phishing and illegal content act on the site, not on the VM. Central calls `proxy.suspend_site(name, reason_code)`, and the proxy serves the tombstone for the site:

- The tombstone is a new route value. Today the route value `"-"` means that the site is being prepared.
- The proxy serves a branded "suspended" page with a link to the abuse page: HTTP 403 for abuse, 451 for a legal hold.
- The tombstone stays until Central lifts it.

Each proxy node uploads its access log to the Cargo log bucket every 5 min. See [log upload](contract.md#log-upload).

## Inbound DDoS

The customer is the victim. Central applies `ingress_throttle` to the VM. These steps do not count as abuse history.

Protection that is always on:

- Sites behind the HTTP proxy are shielded.
- SYN cookies and conntrack limits on hosts and proxy nodes.

Operator steps, for an attack that `ingress_throttle` cannot stop:

1. XDP drop on the host uplink for the attacked address. It saves host CPU, not link capacity.
2. IP withdrawal: detach the address, [block it](../networking/public-ips.md#block-an-address), and attach a new address to the VM.
3. A null route on a `/32`, by a ticket to the transit provider.

As far as we know, no transit provider gives us a remote triggered blackhole. Scrubbing, for example Cloudflare Magic Transit, is a cost decision for when attacks become common.

::: details Code to change

- [Network convergence](../../metal/internal/network/linux_allocator.go) renders the namespace `filter` table. The `metal_abuse` chain goes there.
- [Firewall rendering](../../metal/internal/network/firewall.go) renders the tenant firewall after `metal_abuse`.
- [Proxy router](../../services/http-proxy/nginx/lua/http/router.lua) handles the `"-"` route value.

:::
