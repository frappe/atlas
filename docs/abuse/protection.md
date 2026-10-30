# Core service protection

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Core service protection keeps Metal hosts and Atlas service VMs up when an attacker or a tenant VM sends floods, scans or spoofed packets. These are static platform rules. They are not restrictions, they have no case in Central, and Central does not decide them. Restrictions on tenant VMs are in [enforcement](enforcement.md).

## Scope

- Metal hosts.
- The Atlas service VMs: the [HTTP proxy](../networking/http-proxy/index.md), the [IPv6 router](../networking/ipv6-router.md) and the [WireGuard gateway](../networking/wireguard-gateway.md). See [service VMs](../region/service-vms.md).
- `control-cluster` is not a separate VM. It is the Python package `atlas_control` in the HTTP proxy and WireGuard gateway VMs. The rules of those VMs cover its cluster port.
- Other tenant-0 VMs. Cargo Garage nodes take the same baseline. See [Cargo network protection ↗](https://github.com/frappe/cargo/blob/develop/docs/object-storage.md#network-protection).
- The [VM guard](#vm-guard) applies to every VM on a host. The [per-VM conntrack limit](#conntrack) applies to tenant VMs only.

## Rollout

Each new nft rule on this page has two modes:

- `count`: nft counts the packets that match the rule, and does not drop them.
- `drop`: nft drops the packets that match the rule, and counts them.

Where the mode is set:

- Hosts: `[protect] mode` in `metald.toml`.
- HTTP proxy: `[protect] mode` in `/etc/atlas/proxy-control.toml`.
- WireGuard gateway: `[protect] mode` in `/etc/atlas/wireguard-gateway.toml`.
- IPv6 router: it has no config file today. Its setup writes `/etc/atlas/ipv6-router.toml` with only this key.

Atlas writes the key from the "Protection" section of Atlas Settings, one mode for each service. Layout: [Atlas Settings](layouts.md#atlas-settings-protection-layout-change). Atlas writes the `metald.toml` value in the same way as `abuse_metrics`, and the service values with the service config. The IPv6 router gets the value on the next "Update Package".

`[protect] exempt_prefixes` and `[abuse] conntrack_entries_per_vm` are optional. Atlas does not write them. A host or service without them uses the defaults.

The default mode is `count`. An operator sets `drop` for one service at a time, after at least 7 days of counters. Before the change, the operator examines the counters for drops of real traffic. See [counters and alerts](#counters-and-alerts).

The [VM guard](#vm-guard) and the [sysctl baseline](#host-and-service-vm-baseline) have no `count` mode. They apply at once.

The rate values on this page are start values. An operator calibrates them in `count` mode. The 7-day check names the sources that would be dropped, and the operator examines carrier CGNAT addresses first, because many mobile users share one address. See [HTTP proxy](enforcement.md#http-proxy).

Where the rules attach:

- The meters for [SYN floods](#syn-floods), [UDP floods](#udp-floods), [ICMP floods](#icmp-floods), [inbound port scans](#inbound-port-scans) and [TLS handshake floods](#tls-handshake-floods) are on the `input` hook only. They protect the host or the service VM itself. They never apply to tenant traffic that a router, a gateway or a host forwards.
- The [bogon and reverse path rules](#spoofed-sources) are on `prerouting` and match `iif` of the public or uplink interface only.
- The [fragment rules](#fragments) are on `prerouting` at priority `-450`, and match only the addresses of the host or the service.
- The IPv6 router `forward` chain in `router.nft` does not change, except for the [IPv6 router](#ipv6-router) NDP limits.

## Host and service VM baseline

Each host and service VM gets these values in a file in `/etc/sysctl.d/`:

| Value | Setting |
| --- | --- |
| `net.ipv4.tcp_syncookies` | `1`. It also covers IPv6 TCP. The kernel default is `1`. The file sets it so that no other file can change it. |
| `net.ipv4.tcp_max_syn_backlog` | `8192` |
| `net.netfilter.nf_conntrack_max` | 65,536 for each GiB of memory, and never less than the kernel default |
| `net.ipv4.ipfrag_time` | `15` (seconds). The kernel default is 30. |
| `net.ipv6.ip6frag_time` | `15` (seconds). The kernel default is 60. |

- The fragment memory limits (`ipfrag_high_thresh`, `ip6frag_high_thresh`) keep the kernel defaults of 4 MiB.
- `nf_conntrack_buckets` is read-only in `sysctl`. The setup sets the hash size with `options nf_conntrack hashsize=<N>` in `/etc/modprobe.d/`, a quarter of `nf_conntrack_max`.

- Hosts: [install-metald.sh](../../atlas/scripts/install-metald.sh) adds the values to `99-metald.conf`. Today that file holds only `net.ipv4.ip_forward` and the `accept_ra` value of the public interface.
- Service VMs: each setup script adds the values to its own file. Today [ipv6-router setup.sh](../../services/ipv6-router/setup.sh) and [wg-gateway setup.sh](../../services/wg-gateway/setup.sh) write only `net.ipv6.conf.all.forwarding`. The [HTTP proxy setup.sh](../../services/http-proxy/nginx/setup.sh) writes no sysctl file.
- The `nf_conntrack` module must be loaded before `sysctl` applies the file. Each setup adds it to `/etc/modules-load.d/`.
- Today no host or service VM sets any of these values. The kernel defaults apply. On a 64-bit machine with more than 4 GiB of memory, the default `nf_conntrack_max` is 1,048,576 or less, from the kernel formula. Check the value on each host before the change.

## SYN floods

- SYN cookies are on. See the [baseline](#host-and-service-vm-baseline).
- An nft meter counts new TCP connections from each source: one IPv4 address, or one IPv6 `/64`.
- Above 200 new connections a second, with a burst of 400, nft drops the new connections of that source.
- Exempt prefixes:
  - The mesh and platform prefix list. It is the same list as the [`tc` policer](../networking/host-networking.md#throughput-limits) uses: RFC 1918, `fc00::/7`, `100.64.0.0/10` and the platform ranges.
  - An operator list in the service config: `[protect] exempt_prefixes` in `metald.toml` or in the service config file. See [rollout](#rollout).
- The Insights chart "TCP SynCookie" in [network.py](../../scripts/insights/host/network.py) already shows the cookies that a host sends.

## UDP floods

- An nft meter counts UDP packets from each source, on UDP ports that are not WireGuard listen ports.
- Above 2,000 packets a second, with a burst of 4,000, nft drops the packets of that source.
- WireGuard listen ports use the rules in [WireGuard](#wireguard).
- The exempt prefixes are the same as in [SYN floods](#syn-floods).

## ICMP floods

- nft drops ICMP and ICMPv6 echo requests from one source above 10 a second, with a burst of 20.
- Only echo request has a limit.
- nft never drops the ICMPv6 messages that IPv6 needs ([RFC 4890](https://www.rfc-editor.org/rfc/rfc4890)):
  - Destination Unreachable (type 1).
  - Packet Too Big (type 2).
  - Time Exceeded (type 3).
  - Parameter Problem (type 4).
  - NDP types 133 to 137, only with hop limit 255.
- The ICMPv4 types that path MTU discovery needs (type 3) also stay.

## Conntrack

- Each host and service VM sets `nf_conntrack_max`. See the [baseline](#host-and-service-vm-baseline).
- The Insights check "Conntrack table used, %" in [alerts.py](../../scripts/insights/alerts.py) already alerts when a host uses more than 80% of its table. No new alert is necessary.

Per-VM limit on hosts:

- Each tenant VM has at most 65,536 conntrack entries. `metald.toml` sets the limit with `[abuse] conntrack_entries_per_vm`.
- Tenant 0 VMs and network gateway VMs (`is_network_gateway`) have no per-VM limit. They carry the flows of many customers. The host table limit and its alert still apply to them.
- Metal renders the limit with nft `ct count` in the `filter` table of the VM namespace, before the [`metal_abuse` chain](enforcement.md#metal_abuse-chain).
- Above the limit, nft drops new flows of the VM in both directions.
- Each drop adds 1 to `vm_abuse_conntrack_limited_total`. See [counters](metrics.md#counters).
- The limit stops one VM from filling the host table and blocking the flows of other VMs.

## Spoofed sources

- On public interfaces, nft drops packets with a bogon source address.
- The bogon list is static. It comes from [RFC 6890](https://www.rfc-editor.org/rfc/rfc6890) and the [IANA IPv6 special-purpose address registry](https://www.iana.org/assignments/iana-ipv6-special-registry/). It is not a live feed. A change to the list is a code change.
- A reverse path check, `fib saddr . iif oif missing drop`, drops a packet when the source address does not route back through the interface that received it. The rule also covers IPv6.
- The check runs on the IPv6 router, the WireGuard gateway and the public interface of each host.
- A host with more than one uplink uses loose mode, `fib saddr oif missing drop`. In loose mode, any route to the source is sufficient.
- The check accepts the unspecified source `::`, so duplicate address detection on the uplink still works.

## Inbound port scans

- On the public interfaces of hosts and service VMs, nft counts new TCP and UDP packets from each source to closed ports.
- A closed port is a port that is not in the open port list of the host or the service.
- A source that sends more than 20 such packets a minute goes into an nft set with a timeout of 1 h.
- nft drops all packets from a source in the set.
- The exempt prefixes are the same as in [SYN floods](#syn-floods).
- Each drop adds 1 to `atlas_protect_scan_blocked_total`.

## Fragments

- The kernel drops overlapping fragments during reassembly.
- nft drops TCP fragments to the addresses of the host or the service: IPv4 packets with `ip frag-off & 0x1fff != 0` or the more-fragments flag and protocol TCP, and IPv6 packets with `exthdr frag exists` and next header TCP. TCP does not need fragments. A later fragment has no TCP header, so the rule cannot match ports.
- nft drops a first fragment that does not hold the complete transport header: shorter than the IP header and 20 bytes for TCP, or 8 bytes for UDP.
- UDP fragments stay allowed.
- When conntrack is loaded, the kernel reassembles fragments before the usual nft hooks. So the fragment rules are in a `prerouting` chain with priority `-450`, before the defragmentation hook at `-400`.

## TLS handshake floods

- On proxy nodes, an nft meter counts new connections to port 443 from each source.
- Above 50 new connections a second, with a burst of 100, nft drops the new connections of that source.
- The exempt prefixes are the same as in [SYN floods](#syn-floods).

Handshake timeout:

- The HTTP module of OpenResty 1.29.2.5 ([setup.sh](../../services/http-proxy/nginx/setup.sh)) has no `ssl_handshake_timeout` directive. That directive is only in the stream SSL module.
- The proxy terminates TLS in its local HTTP servers on `127.0.0.1:8443` and `127.0.0.1:8446`. There, `client_header_timeout 15s` in [nginx.conf](../../services/http-proxy/nginx/nginx.conf) also limits the TLS handshake.
- The public port 443 is a stream server with `ssl_preread`. It does not terminate TLS. It waits for the ClientHello for `preread_timeout`, which is not set today, so the default of 30 s applies. Set `preread_timeout 10s` on this server.

The request limits of the proxy are in [enforcement](enforcement.md#http-proxy).

## WireGuard

- The rules apply on the WireGuard listen ports: the port of each WireGuard gateway node and the mesh port of each host.
- nft drops handshake initiation packets from one source address above 20 a second, with a burst of 40. A handshake initiation is message type 1, 148 bytes.
- Data packets, message type 4, have no limit.
- Under load, WireGuard also sends its own cookie reply. This gives more protection.
- These rules protect the listen port. Abuse metrics still never read a WireGuard interface.

Peers for each tenant:

- Central adds peers with the gateway peer API, `POST /v1/peers` ([peers.py](../../services/wg-gateway/daemon/gatewayd/peers.py)).
- Today the gateway has no peer limit for each tenant. Only the `client_id` range, 1 to 65,535, limits the peers of one tenant.
- The gateway gets a limit: at most 256 peers for each tenant, from `[protect] peers_per_tenant` in `/etc/atlas/wireguard-gateway.toml`. Above it, the API returns an error and adds no peer. A table restore with `PUT /v1/peers` does not apply the limit.

## IPv6 router

NDP answers:

- Answer NDP only for addresses that are assigned to VMs.
- This is true today. The IPv6 router answers no NDP for its block, because the provider routes the block to the router. Metal hosts answer proxy NDP only for the entries in the proxy table: an attached public `/128` on the public interface (`claimPublicIPv6` in [nat.go](../../metal/internal/network/nat.go)), and the mesh address of each VM ([mesh.go](../../metal/internal/network/mesh.go), and the uplink setting in [host.go](../../services/wg-mesh/cli/host.go)).

Neighbor Solicitations:

- nft limits Neighbor Solicitations from the uplink to 1,000 a second in total, and 50 a second from each source.
- The rule applies on the uplink of the IPv6 router and on the public interface of each host.

Neighbor table size:

- No file sets `net.ipv6.neigh.default.gc_thresh1`, `gc_thresh2` or `gc_thresh3` today. The kernel defaults are 128, 512 and 1,024.
- Set them from the VM count, with margin:
  - `gc_thresh1`: 1,024.
  - `gc_thresh2`: 2,048, plus 4 for each VM that the host can hold. On a host, that is the [NAT slot](metrics.md#nat-port-ranges) count.
  - `gc_thresh3`: 2 times `gc_thresh2`.
- Hosts get the values in `99-metald.conf`. The IPv6 router gets them in `90-atlas-ipv6-router.conf`, with the VM count of the region.
- These are the IPv6 neighbor values. The IPv4 ARP values (`net.ipv4.neigh.default.gc_thresh*`) get the same numbers.

## VM guard

At the VM tap, Metal drops these packets from the VM:

- A source address that is not an address of the VM:
  - IPv4: any source other than the guest address `172.16.0.2`.
  - IPv6: any source other than the mesh address of the VM, its dedicated public IPv6 address, an address of its delegated block, or a link-local address (`fe80::/10`).
- Router Advertisements (ICMPv6 type 134) and Redirects (type 137).
- Neighbor Advertisements (type 136) for a target that is not an address of the VM.
- DHCPv4 server replies (UDP source port 67) and DHCPv6 server replies (UDP source port 547).

Rules:

- A network gateway VM (`is_network_gateway`) skips the source check, because it forwards packets with client addresses. The other guard rules apply to it.
- Each drop adds 1 to `vm_abuse_guard_dropped_total{rule}`. See [counters](metrics.md#counters).
- The guard has no `count` mode. See [rollout](#rollout).

Where the guard goes:

- The guard is a separate TCX program on the tap ingress in the VM namespace. Metal attaches it first, before the counter program in [track_traffic.c](../../metal/internal/network/traffic/bpf/track_traffic.c). So the counters see only the packets that the guard accepts. A spoofing VM shows only in `vm_abuse_guard_dropped_total`, which the operator alert reads. Watcher does not read it.
- `track_traffic.c` only counts. It drops nothing.
- The WG Mesh program `handle_vm_packet` ([vm.h](../../services/wg-mesh/bpf/vm.h)) runs with `tc` on the ingress of the host side of the VM interface. It is a different interface, so it does not conflict with the guard.

What exists today:

- `handle_vm_packet` already checks the IPv6 source for mesh destinations and for destinations that a gateway carries. A VM must own the source, and the tenants must be able to communicate.
- Nothing checks IPv4 sources, IPv6 sources to other public destinations, Router Advertisements, Redirects, Neighbor Advertisements or DHCP server replies from a VM.

## Counters and alerts

Counters:

| Metric | Labels | Value |
| --- | --- | --- |
| `atlas_protect_dropped_total` | `service`, `rule` | Packets that a host or service rule matched. In `count` mode, the packets that the rule would drop. |
| `atlas_protect_scan_blocked_total` | `service` | Packets from a source in the [port scan](#inbound-port-scans) set |
| `vm_abuse_conntrack_limited_total` | `tenant_id` | New flows that the [per-VM conntrack limit](#conntrack) dropped |
| `vm_abuse_guard_dropped_total` | `rule`, `tenant_id` | Packets that the [VM guard](#vm-guard) dropped |

- `service` is `metal`, `http-proxy`, `ipv6-router` or `wireguard-gateway`. Cargo uses `garage`.
- `rule` is the section of the rule, for example `syn`, `udp`, `icmp`, `fragment`, `tls`, `wireguard`, `ndp` or `spoofed`. For the VM guard: `source`, `router-advertisement`, `redirect`, `neighbor-advertisement` or `dhcp`.
- `tenant_id` comes from the mesh address of the VM, which holds the tenant ID. See [gateways](../networking/wg-mesh/gateways.md). Metal does not otherwise know the tenant.
- Hosts export the counters with the metald telemetry. Service VMs export them with Vector, which they already run. A Vector `exec` source reads `nft --json list counters` every 30 s. See [telemetry](../operate/telemetry.md). The per-VM counters also need `abuse_metrics`. See [counters](metrics.md#counters).

Alerts are Insights checks in [alerts.py](../../scripts/insights/alerts.py). They use its helpers:

- Host rules: a `growth_check` on `atlas_protect_dropped_total` in `P2_HOSTS`. More than 10,000 drops in the last hour, by service and rule.
- Service rules: a check in `P2_PLATFORM`, next to the other service node checks. More than 10,000 drops in the last hour, by service and rule.
- VM rules: a check on `vm_abuse_guard_dropped_total` and `vm_abuse_conntrack_limited_total`. More than 100 drops in the last hour. These series are in the per-VM Datum table, not in `samples`, so the Insights user needs read access to that table. The check is silent until an operator turns on `is_abuse_metrics_enabled`.
- The alert text names the VM, the tenant and the rule.
