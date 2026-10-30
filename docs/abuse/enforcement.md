# Abuse enforcement

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Central decides each restriction and calls the Atlas [restriction API](contract.md#restriction-api). Static protection rules for hosts, service VMs and the VM tap are not restrictions. They are in [core service protection](protection.md). Atlas keeps no restriction record. It puts the restriction in the `restrictions` list of the VM network object in Metal. Metal renders it in the VM namespace. Metal and Atlas never restrict a VM by themselves.

The ladder, lifts and policy are in the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md). The state of each restriction is in [Central restriction states ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#restriction-states).

## Restrictions in the network object

Atlas keeps no copy of per-VM network state. Metal holds the desired VM spec. A restriction change uses the same path as a route or firewall change, in `VirtualMachineService` ([vm_service.py](../../atlas/vm/core/vm_service.py)):

1. Atlas takes the per-VM file lock `atlas:vm-network:<vm>` (`network_lock`, 10 s timeout). It is not a row lock, so no database lock waits on Metal.
2. `update_network` reads `desired.network` from Metal.
3. Atlas merges one change. It adds, changes or removes one entry, or sets `is_mail_ports_blocked`. It also removes the entries with an `expires_at` in the past.
4. Atlas sends the complete network with `PUT /v1/vms/{id}/network`. See [updates replace, not merge](../interfaces/metal-contract.md#updates-replace-not-merge).
5. Metal validates the spec and saves it with a new generation before it replies.
6. Metal renders the spec in `LinuxAllocator.Ensure`, reapplies it after a restart, and audits it every minute. See [how Metal applies a firewall change](../networking/host-networking.md#how-metal-applies-a-firewall-change).

Progress is the desired generation against the observed generation. A host failure is `observed.error`. Atlas returns both in the [restriction API](contract.md#restriction-api) responses and in `GET` VM. Host sync copies them to Virtual Machine State, and Atlas sends the [`vm.network` event](contract.md#events-to-central) to Central when they change.

`update_network` copies each network field by name into the request. It must also copy `restrictions` and `is_mail_ports_blocked`. If it does not, a route, firewall or address change removes them.

## metal_abuse chain

`metal_abuse` is an nft chain that the operator owns. It is in the `filter` table of each VM namespace, and nft evaluates it before the [tenant firewall](../networking/host-networking.md#firewall-rules). `LinuxAllocator.Ensure` renders it on every reconcile pass, so an interface restart or a tenant firewall change cannot remove it.

The chain holds the `mail_ports` block and the active entries of `restrictions`. Throttles are `tc` policers next to the [throughput limits](../networking/host-networking.md#throughput-limits). On each pass, Metal skips an entry after its `expires_at`.

The `mail_ports` block applies to a VM whose network has `is_mail_ports_blocked: true`:

- Metal drops new TCP flows from the VM to remote ports 25, 465 and 587.
- Metal drops new TCP flows from a remote to VM ports 25, 465 and 587.
- It applies only to remote addresses outside the mesh prefix list. Mesh traffic is not affected. The list is the `mesh` scope in [counters](metrics.md#counters).
- A missing `is_mail_ports_blocked` means open. Existing VMs keep the mail ports open. There is no Frappe patch and no metald patch, and the Atlas default does not change.
- Central sets `is_mail_ports_blocked: true` in every VM create request. It clears and sets the field with the [mail ports API](contract.md#mail-ports-api).
- It is not a restriction entry and not abuse history.
- Each drop adds 1 to `vm_abuse_mail_blocked_total`, with `direction` `outbound` or `inbound`. See [counters](metrics.md#counters).

The rules for a mail ports unblock are Central rules.

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
- A new server starts restricted when Central sends `restrictions` and `is_mail_ports_blocked` in VM create. Both are part of the create fingerprint. See [create and retry](../interfaces/metal-contract.md#create-and-retry).

The tenant API cannot write restrictions:

- Power actions are never blocked.
- Route and firewall changes are accepted, but they cannot widen egress. `metal_abuse` runs before the tenant firewall, and `update_network` keeps the entries.
- `GET` VM returns `restrictions`, `is_mail_ports_blocked` and the generations, read from Metal, like routes.
- The restriction and mail ports routes refuse a VM of tenant 0 with `403`.

## Operator emergency restriction

An `Atlas Admin` can restrict a VM without Central, for example while Central is down:

1. The operator uses the Desk action on the Virtual Machine form. Layout: [Virtual Machine](layouts.md#virtual-machine-layout-change).
2. Atlas uses the same lock and the same read, merge and replace path. The entry ID is `atlas-` and a random suffix.
3. Atlas adds a Comment on the VM with the user, the kind and the reason.
4. The desired generation moves, so Atlas sends the [`vm.network` event](contract.md#events-to-central). Central reads `GET` VM, finds the entry and posts it to `#abuse`. Central links the entry to a case. The Central repair scan finds the entry too, if the event is lost.

An `Atlas Admin` can also isolate every VM of one tenant:

1. The operator uses the "Isolate Tenant" Desk action on the Virtual Machine list, with a tenant ID and a reason. Layout: [Virtual Machine](layouts.md#virtual-machine-layout-change).
2. Atlas refuses tenant 0.
3. Atlas makes one entry ID, `atlas-` and a random suffix, for the action.
4. A background job applies an `isolate` entry with that ID to each VM of the tenant that is not terminating, in all states, Paused and Stopped included. It does one VM at a time, with the same lock and the same read, merge and replace path.
5. Atlas adds a Comment on each VM with the user, the entry ID and the reason. When a VM fails, for example on an old Metal or a lock timeout, the job goes to the next VM, and the Comment on the failed VM records the error.
6. Each VM sends its own `vm.network` event.

- "Lift Tenant Isolation" takes the tenant ID and the entry ID. It lifts that entry on each VM of the tenant, in the same way.
- The action does not change new VMs of the tenant. Central decides about new servers.

Who changes a restriction:

| Path | Actor | Record |
| --- | --- | --- |
| Normal | Central, through the `central_only` [restriction API](contract.md#restriction-api) | The Central Resource Action and the case |
| Emergency | An `Atlas Admin`, with the Desk action for one VM or one tenant | The Comment on the VM. Central adds its own row from the `vm.network` event or the repair scan. |
| Expiry | None. Metal skips the entry after `expires_at`. | Central knows the end from its own `expires_at`. |

## HTTP proxy

Static rate limits, for each client address and site:

- `limit_req`: 100 requests a second, with a burst of 500.
- `limit_conn`: 200 connections.

The values are high on purpose, because Indian mobile networks put many users behind one CGNAT address. Today [nginx.conf](../../services/http-proxy/nginx/nginx.conf) has no `limit_req` and no `limit_conn`.

A limit for each host:

- `limit_req_zone $host`: 1,000 requests a second, with a burst of 2,000.
- Above the limit, the proxy returns HTTP 429, with `limit_req_status 429`. The nginx default is 503.

Slow clients:

- `client_header_timeout` and `client_body_timeout` are 15 s today, in [nginx.conf](../../services/http-proxy/nginx/nginx.conf). They stop slow header and slow body attacks. There is no change.
- TLS handshake floods are in [core service protection](protection.md#tls-handshake-floods).

Site tombstone. Phishing and illegal content act on the site, not on the VM. Central calls `proxy.suspend_site(name, reason_code)`, and the proxy serves the tombstone for the site:

- The tombstone is a new route value. Today the route value `"-"` means that the site is being prepared.
- The proxy serves a branded "suspended" page with a link to the abuse page: HTTP 403 for abuse, 451 for a legal hold.
- The tombstone stays until Central lifts it.

Each proxy node uploads its access log to the Cargo log bucket every 5 min. See [log upload](contract.md#log-upload).

## Inbound DDoS

The customer is the victim. Central applies `ingress_throttle` to the VM. These steps do not count as abuse history.

Protection that is always on:

- Sites behind the HTTP proxy are shielded.
- SYN cookies, conntrack limits and per-source meters on hosts and service VMs. See [core service protection](protection.md).

Operator steps, for an attack that `ingress_throttle` cannot stop:

1. XDP drop on the host uplink for the attacked address. It saves host CPU, not link capacity.
2. IP withdrawal: detach the address, [block it](../networking/public-ips.md#block-an-address), and attach a new address to the VM.
3. A null route on a `/32`, by a ticket to the transit provider.

As far as we know, no transit provider gives us a remote triggered blackhole. Scrubbing, for example Cloudflare Magic Transit, is a cost decision for when attacks become common.

::: details Code to change

- [VM service](../../atlas/vm/core/vm_service.py): `update_network` copies `restrictions` and `is_mail_ports_blocked`. The restriction and mail ports changes run under `network_lock`.
- [Metal network request validation](../../metal/internal/api/vm_request.go) validates the entries.
- [Network convergence](../../metal/internal/network/linux_allocator.go) renders the namespace `filter` table. The `metal_abuse` chain goes there.
- [Firewall rendering](../../metal/internal/network/firewall.go) renders the tenant firewall after `metal_abuse`.
- [Proxy router](../../services/http-proxy/nginx/lua/http/router.lua) handles the `"-"` route value.
- [Proxy config](../../services/http-proxy/nginx/nginx.conf) gets the `limit_req` and `limit_conn` zones and the zone for each host.

:::
