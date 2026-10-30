# Abuse enforcement

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Central decides each restriction and calls the Atlas [restriction API](contract.md#restriction-api). Atlas keeps no restriction record. It puts the restriction in the `restrictions` list of the VM network object in Metal. Metal renders it in the VM namespace. Metal and Atlas never restrict a VM by themselves.

The ladder, lifts and policy are in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md). The state of each restriction is in [Central restriction states ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#restriction-states).

## Restrictions in the network object

Atlas keeps no copy of per-VM network state. Metal holds the desired VM spec. A restriction change uses the same path as a route or firewall change, in `VirtualMachineService` ([vm_service.py](../../atlas/vm/core/vm_service.py)):

1. Atlas takes the per-VM file lock `atlas:vm-network:<vm>` (`network_lock`, 10 s timeout). It is not a row lock, so no database lock waits on Metal.
2. `update_network` reads `desired.network` from Metal.
3. Atlas merges one change. It adds, changes or removes one entry, or sets `is_port_25_blocked`. It also removes the entries with an `expires_at` in the past.
4. Atlas sends the complete network with `PUT /v1/vms/{id}/network`. See [updates replace, not merge](../interfaces/metal-contract.md#updates-replace-not-merge).
5. Metal validates the spec and saves it with a new generation before it replies.
6. Metal renders the spec in `LinuxAllocator.Ensure`, reapplies it after a restart, and audits it every minute. See [how Metal applies a firewall change](../networking/host-networking.md#how-metal-applies-a-firewall-change).

Progress is the desired generation against the observed generation. A host failure is `observed.error`. Atlas returns both in the [restriction API](contract.md#restriction-api) responses and in `GET` VM.

`update_network` copies each network field by name into the request. It must also copy `restrictions` and `is_port_25_blocked`. If it does not, a route, firewall or address change removes them.

## metal_abuse chain

`metal_abuse` is an nft chain that the operator owns. It is in the `filter` table of each VM namespace, and nft evaluates it before the [tenant firewall](../networking/host-networking.md#firewall-rules). `LinuxAllocator.Ensure` renders it on every reconcile pass, so an interface restart or a tenant firewall change cannot remove it.

The chain holds the `smtp_outbound` block and the active entries of `restrictions`. Throttles are `tc` policers next to the [throughput limits](../networking/host-networking.md#throughput-limits). On each pass, Metal skips an entry after its `expires_at`.

The `smtp_outbound` block:

- Metal drops new TCP flows to port 25 on public destinations for a VM whose network has `is_port_25_blocked: true`.
- A missing `is_port_25_blocked` means open. Existing VMs keep port 25. There is no Frappe patch and no metald patch, and the Atlas default does not change.
- Central sets `is_port_25_blocked: true` in every VM create request. It clears and sets the field with the [port 25 API](contract.md#port-25-api).
- It is not a restriction entry and not abuse history.
- Each drop adds 1 to `vm_abuse_smtp_blocked_total`. See [counters](metrics.md#counters).
- Submission ports 465 and 587 stay open.

The rules for a port 25 unblock are Central rules.

## Restriction kinds

Each active entry of `restrictions` renders one kind:

| Kind | Parameters | Rendering |
| --- | --- | --- |
| `egress_throttle` | `pps`, `bps` | `tc` policer on public egress |
| `ingress_throttle` | `pps`, `bps` | `tc` policer on traffic to the VM. See [inbound DDoS](#inbound-ddos). |
| `port_block` | `ports`: a list of `{port, protocol}` objects | Drop new outbound flows to the listed ports |
| `quarantine` | None | Drop new outbound flows, except DNS (53) and HTTPS to the package sources: Ubuntu mirrors, PyPI and npm, not GitHub. Inbound traffic from the WireGuard gateway and the mesh stays. |
| `isolate` | None | Drop all traffic, except the WireGuard gateway, backups to Cargo object storage, and Central to Pilot admin |

Each entry has these fields:

| Field | Value |
| --- | --- |
| `id` | The `idempotency_key` from Central, which is the name of the Central Resource Action. An [operator entry](#operator-emergency-restriction) has `atlas-` and a random suffix. |
| `kind` | One kind from the table above |
| `parameters` | `ports` for `port_block`. `pps` and `bps` for a throttle. Empty for `quarantine` and `isolate`. |
| `expires_at` | Optional. Metal skips the entry after this time. |
| `case_reference` | The Central case, for example `AC-00192`. Empty for an operator entry. |

- `port_block` takes explicit ports only. Central turns a [port class](metrics.md#port-classes) into ports.
- The package sources of `quarantine` are a fixed list in metald.
- All active entries apply together. For the same traffic, the most restrictive one wins.
- A lift of one entry never lifts another.
- Metal skips an expired entry by itself, so a restriction ends when Atlas is down. Atlas removes the expired entry on its next read, merge and replace. No Atlas job runs at expiry.
- Restrictions are part of the network object, so they move with the VM on migration.
- A new server starts restricted when Central sends `restrictions` and `is_port_25_blocked` in VM create. Both are part of the create fingerprint. See [create and retry](../interfaces/metal-contract.md#create-and-retry).

The tenant API cannot write restrictions:

- Power actions are never blocked.
- Route and firewall changes are accepted, but they cannot widen egress. `metal_abuse` runs before the tenant firewall, and `update_network` keeps the entries.
- `GET` VM returns `restrictions`, `is_port_25_blocked` and the generations, read from Metal, like routes.
- The restriction and port 25 routes refuse a VM of tenant 0 with `403`.

## Operator emergency restriction

An `Atlas Admin` can restrict a VM without Central, for example while Central is down:

1. The operator uses the Desk action on the Virtual Machine form. Layout: [Virtual Machine](layouts.md#virtual-machine-layout-change).
2. Atlas uses the same lock and the same read, merge and replace path. The entry ID is `atlas-` and a random suffix.
3. Atlas adds a Comment on the VM with the user, the kind and the reason.
4. Central finds the entry in its repair scan of `GET` VM and posts it to `#abuse`. Central links the entry to a case.

Who changes a restriction:

| Path | Actor | Record |
| --- | --- | --- |
| Normal | Central, through the `central_only` [restriction API](contract.md#restriction-api) | The Central Resource Action and the case |
| Emergency | An `Atlas Admin`, with the Desk action | The Comment on the VM. Central adds its own row from the repair scan. |
| Expiry | None. Metal skips the entry after `expires_at`. | Central knows the end from its own `expires_at`. |

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

- [VM service](../../atlas/vm/core/vm_service.py): `update_network` copies `restrictions` and `is_port_25_blocked`. The restriction and port 25 changes run under `network_lock`.
- [Metal network request validation](../../metal/internal/api/vm_request.go) validates the entries.
- [Network convergence](../../metal/internal/network/linux_allocator.go) renders the namespace `filter` table. The `metal_abuse` chain goes there.
- [Firewall rendering](../../metal/internal/network/firewall.go) renders the tenant firewall after `metal_abuse`.
- [Proxy router](../../services/http-proxy/nginx/lua/http/router.lua) handles the `"-"` route value.

:::
