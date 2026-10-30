# Abuse metrics

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Metal counts the traffic of each VM and exports the counts to Datum as metrics. Metal also writes the SNI log, the flow log and the NAT port range log, and uploads them to the Cargo log bucket. Metal does not compare a count with a threshold, and it does not make a finding.

Watcher, a Cargo module in each region, reads the metrics and the logs, finds abuse and sends findings to Central. Its inputs are in [Watcher inputs ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#inputs). Finding codes, thresholds, windows and feeds are in [Watcher finding codes ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#finding-codes). Profiles are in [Central profiles ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#profiles).

Metal never reads the content of guest traffic. It reads only the TLS ClientHello and, for the [stratum match](#stratum-match), the first payload bytes of a new flow. The [flow log](#flow-log) reads packet headers only. The acceptable use policy (AUP) and the privacy policy must say so.

## Counters

The counters extend the existing packet monitor: [monitor.go](../../metal/internal/network/traffic/monitor.go) and the TCX programs in [track_traffic.c](../../metal/internal/network/traffic/bpf/track_traffic.c). There is no second TCX program. New maps sit next to the existing maps. The maps belong to Metal, so they need no WG Mesh map layout patch.

The `track_traffic.c` maps are not pinned. metald creates them in memory when it starts (`bpfHooks.open` in [bpf_hooks.go](../../metal/internal/network/traffic/bpf_hooks.go)), and gives them to each program with `MapReplacements`. So a value layout change needs no metald patch. New maps are still the rule, so that the existing values keep their layout.

Metal counts only the traffic that the guest starts. It never counts a reply as a new flow. Today `tcp_syn_packets` in `track_traffic.c` counts each packet with the SYN flag, a SYN-ACK too. That counter keeps its meaning for its current readers, for example `sent_tcp_syn_packets` in the VM metrics API. A new counter counts a SYN without ACK, for `vm_abuse_tcp_initiated_total`.

Metal exports the `vm_abuse_*` metrics only when `metald.toml` has `[telemetry] abuse_metrics = true`. The default is false. Atlas writes the value from the Atlas Settings check `is_abuse_metrics_enabled`. See [telemetry](contract.md#telemetry).

Metal exports the counters at the metald telemetry interval, 30 s by default. Each series has the label `virtual_machine`, with the Atlas VM ID, for example `vm-0a1b2c3d4e`. Metal exports the counters of every VM, tenant-0 VMs included.

The metald telemetry registry rejects samples with churning labels, `virtual_machine` included. The registry exempts metrics with the `vm_abuse_` prefix from this check, because Datum stores them in a table with a 30-day TTL. All other metrics keep the check.

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `vm_abuse_transmit_packets_total` | Counter | `scope`, `protocol` | Packets that the VM sends |
| `vm_abuse_transmit_bytes_total` | Counter | `scope`, `protocol` | Bytes that the VM sends |
| `vm_abuse_receive_packets_total` | Counter | `scope`, `protocol` | Packets that the VM receives |
| `vm_abuse_receive_bytes_total` | Counter | `scope`, `protocol` | Bytes that the VM receives |
| `vm_abuse_tcp_initiated_total` | Counter | None | SYN without ACK to public destinations |
| `vm_abuse_tcp_answered_total` | Counter | None | Initiated TCP flows that got a SYN-ACK |
| `vm_abuse_udp_first_packets_total` | Counter | None | UDP packets to a public destination that is not in the flow table |
| `vm_abuse_destinations` | Gauge | `port_class`, `window` | Distinct public destinations in the window |
| `vm_abuse_ports_per_destination_max` | Gauge | None | The highest number of distinct destination ports to one public address in the last 60 s |
| `vm_abuse_flows_per_destination_max` | Gauge | `port_class` | The highest number of new flows to one public address in the last 10 min |
| `vm_abuse_unanswered_pairs` | Gauge | None | Distinct public address and port pairs with no answer in the last 1 h |
| `vm_abuse_smtp_blocked_total` | Counter | None | New TCP flows to port 25 that the [`smtp_outbound` block](enforcement.md#metal_abuse-chain) dropped |
| `vm_abuse_stratum_flows_total` | Counter | None | New flows with a [stratum match](#stratum-match) |
| `vm_abuse_amplification_bytes_total` | Counter | `direction`, `port` | Public UDP bytes on a port in the `amplification` class |
| `vm_abuse_flow_rows_dropped_total` | Counter | None | New flows that got no [flow log](#flow-log) entry, because the VM was at its entry cap |
| `vm_abuse_public_address` | Gauge | `address` | 1 for each public address that the VM has for itself |

Labels:

- `scope` is `public` or `mesh`. A `mesh` destination is in the same prefix list as the [`tc` policer](../networking/host-networking.md#throughput-limits): RFC 1918, `fc00::/7`, `100.64.0.0/10` and the platform ranges. All other destinations are `public`. Metal counts both scopes at the VM tap. Metal never reads `wg0` or any other WireGuard interface for abuse data.
- `protocol` is `tcp`, `udp`, `icmp` or `other`.
- `port_class` is a name from [port classes](#port-classes). On `vm_abuse_destinations`, it can also be `all`: every public destination, on any port.
- `window` is `60s` or `10m`.
- `direction` is `transmit` or `receive`. `transmit` counts public UDP bytes that the VM sends from a source port in the `amplification` class. `receive` counts public UDP bytes that the VM receives to a destination port in that class.
- `port` is the port number from the `amplification` class. Watcher uses it to name the protocol.
- `address` is a public IPv4 or IPv6 address that the VM has for itself. It is never the shared host address. Watcher checks these addresses against DNSBLs and shared abuse databases. See [Watcher feeds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#feeds).

Maps:

- A flow table (LRU) for each VM, keyed by destination address, port and protocol. Each entry holds the first-seen time and an answered flag. It holds about 16k entries, 768 KiB for each VM. The answered, first-packet and unanswered counts use it.
- HyperLogLog registers for each port class and window give the distinct destinations.
- The `_max` gauges read the flow table. They report only the busiest destination, not the address.
- The [flow log](#flow-log) has its own maps.

Counters reset when metald restarts. A reader treats a lower value as a reset, not as a negative rate.

Datum keeps other series forever. Datum puts the `vm_abuse_*` series in a separate table with a 30-day TTL. Cargo creates the read-only user `watcher` on the Datum Server for Watcher. See [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics).

These metrics are the only exception to the [telemetry rule](../operate/telemetry.md#metrics) that collectors exclude VM interfaces. The rule stays for all other series.

### Stratum match

Mining pools use the stratum protocol. Metal reads the first payload bytes of a new outbound TCP flow to a port other than 80 and 443. A flow matches when the bytes contain the stratum method `mining.subscribe` or `login`. Metal reads no later bytes of the flow.

Each match adds 1 to `vm_abuse_stratum_flows_total`. Metal does not block the flow.

## Port classes

A port class is a fixed list of ports in metald. Metal uses it only for the `port_class` labels and for the `port` label of `vm_abuse_amplification_bytes_total`. Metal does not block these ports.

| Class | Protocol | Ports |
| --- | --- | --- |
| `auth` | TCP | 21, 22, 23, 445, 1433, 3306, 3389, 5432, 5900, 6379, 27017 |
| `smtp` | TCP | 25 |
| `submission` | TCP | 465, 587 |
| `amplification` | UDP | 53, 123, 161, 389, 1900, 11211 |
| `mining` | TCP | 3333, 4444, 5555, 7777, 14444 |

Central uses this list to turn a class into explicit ports for a [`port_block` restriction](enforcement.md#restriction-kinds). Atlas does not know port classes. A change to the list is a metald change, a Watcher change and a Central change.

## SNI log

The SNI log has one row for each outbound TLS connection: VM, destination address and port, server name (SNI) and time.

- An eBPF program reads only the TLS ClientHello of a new outbound TCP flow.
- On ports 25 and 587, it also reads the ClientHello after STARTTLS.
- It reads no other payload.

Limits:

- No server name for traffic that is not TLS.
- No server name for Encrypted Client Hello (ECH).

Metal records and uploads the SNI log only when `metald.toml` has `[abuse] logs = true`, like the [flow log](#flow-log). The default is false.

SNI rows can be personal data. Keep them in the region. The AUP and the privacy policy must state the collection before an operator turns on `is_abuse_logs_enabled`.

Metal uploads the log to the Cargo log bucket every 5 min. See [log upload](contract.md#log-upload). Watcher reads it with the bucket read key. See [Watcher inputs ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#inputs).

## Flow log

The flow log has rows for the public flows of each VM, one minute at a time. Metal records it at the VM tap, before NAT. So it covers every VM, behind the shared host address or with a dedicated public address.

Metal records and uploads the flow log only when `metald.toml` has `[abuse] logs = true`. The default is false. Atlas writes the value from the Atlas Settings check `is_abuse_logs_enabled`. The [SNI log](#sni-log) uses the same switch. See [log upload](contract.md#log-upload).

Scope:

- Public traffic only. Metal never logs a flow whose remote address is in the `mesh` prefix list. See `scope` in [counters](#counters).
- Metal never reads `wg0` or any other WireGuard interface for the flow log.
- Metal reads packet headers only. It reads no payload for the flow log.

How Metal records it:

- The existing TCX programs at the VM tap, in [track_traffic.c](../../metal/internal/network/traffic/bpf/track_traffic.c), update one LRU hash map of flows.
- An outbound entry has the key: VM, protocol, local address and port, remote address and port.
- An inbound entry has the key: VM, protocol, local port. It has no remote address. For ICMP, the local port field holds the ICMP type. For other protocols that are not TCP or UDP, the local port is 0.
- A separate sketch map holds the remote addresses of each inbound entry. Its key is the VM, the protocol and the local port. Its value is a 256-byte HyperLogLog sketch. The inbound flow entry has no sketch.
- For a packet from a remote, the program first looks for the outbound entry of the reverse direction. If it exists, the packet is a reply, and the program updates that entry. If not, the program updates the inbound entry of the local port.
- The program sets the direction of an entry when it creates the entry. The direction does not change. A reply finds its outbound entry in the same map, so the reverse lookup is reliable while the flow lives.
- Thus a flood with spoofed sources uses one map entry for each local port. It does not fill the map or push out other entries.
- The value holds cumulative counters: the packets, the bytes, the OR of the TCP flags, the first time and the last time. Nothing clears them each minute.
- An outbound flow is a flow that the VM started: its first packet came from the VM. An inbound flow is a flow that a remote started.
- Host cost: one map update for each packet in the existing TCX program, and one map read each minute. Metal copies no packet to user space and reads no conntrack events.

Entry cap for each VM:

- Each VM has at most 10,000 entries in the flow map by default. `metald.toml` sets the cap with `[abuse] flow_entries_per_vm`.
- The program applies the cap when it creates an entry. A per-VM counter map holds the number of entries of each VM. The program adds 1 when it creates an entry. metald subtracts 1 when it deletes an entry.
- At the cap, the program creates no new entry for the VM. It adds 1 to the dropped counter of the VM. metald exports this counter as `vm_abuse_flow_rows_dropped_total`.
- Thus one VM cannot push out the entries of other VMs. LRU eviction occurs only when the sum of the VM caps is more than the map size.
- When the LRU removes an entry, Metal adds 1 to the host counter `metal_abuse_flow_evictions_total`. This counter has no `virtual_machine` label.

Map memory:

- The flow map holds 262,144 entries (256 Ki) by default. `metald.toml` sets the size with `[abuse] flow_map_entries`. Each entry uses about 128 bytes with the key, the value and the hash overhead. The map uses about 32 MiB.
- The sketch map holds 16,384 entries of 256 bytes, about 4 MiB.
- The per-VM counter map is small.

Each minute:

- metald reads the flow map. It writes one row for each entry that changed in the minute. The row holds the change since the last read.
- metald keeps the last values of each entry in memory, to calculate the change.
- metald deletes an entry that has had no packet for 2 min.

Limits:

- A packet that arrives between the read and the delete of an entry is lost.
- The maps are not pinned, so a metald restart loses them. The rows of at most one minute are lost. Flows that continue get new entries. A flow that only receives after the restart is logged as inbound.

Rows:

- Outbound: one row for each outbound entry that changed in the minute.
- Inbound: one row for each inbound entry that changed in the minute. The row has the approximate count of distinct remote addresses, from the sketch map, and no remote address.
- Summary: one row for each VM that was at its cap in the minute. It has the direction `overflow`, and no addresses or ports. `Dropped` holds the count of new flows that got no entry in the minute.
- Thus one VM has at most 10,000 flow rows and one summary row in each minute.

| Field | Value |
| --- | --- |
| Minute | The minute of the read |
| VM | The Atlas VM ID, for example `vm-0a1b2c3d4e` |
| Direction | `outbound`, `inbound` or `overflow` |
| Protocol | `tcp`, `udp`, `icmp` or `other` |
| Local address | The address of the guest at the tap, before NAT. For IPv4 it is the guest address `172.16.0.2`, so the public address of the VM is not in the row. For IPv6 it is the address of the VM. |
| Local port | The port of the guest. On an inbound row, the ICMP type for ICMP, and 0 for other protocols that are not TCP or UDP. |
| Remote address | Outbound only |
| Remote port | Outbound only |
| Distinct remotes | Inbound only. The approximate count of distinct remote addresses. |
| Packets, bytes | The packets and the bytes of the entry since the last read |
| TCP flags | The OR of the TCP flags since Metal created the entry |
| First time, last time | The first packet of the entry, and the last packet before the read |
| Dropped | Summary row only. The count of new flows that got no entry in the minute. |

Metal uploads the log to the Cargo log bucket every 5 min, with the object name `flow`. See [log upload](contract.md#log-upload). Watcher reads it with the bucket read key. See [Watcher inputs ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#inputs).

Storage estimates, at about 20 bytes for each row after zstd. The estimates count flow-minutes: a flow that lasts 60 minutes gives 60 rows. Check them in [Phase 1 ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#phase-1-flow-log-and-counters).

| Case | Rows | Stored |
| --- | --- | --- |
| A normal VM, public traffic only, about 50,000 flow-minutes a day | About 50,000 a day | About 1 MB a day |
| All 58 current VMs, 180 days | | About 10 GB |
| One VM at the cap all day | 14.4 million a day | About 290 MB a day |
| One VM at the cap until Central restricts it, about 3 min from Phase 3 | 30,000 | About 600 KB |
| A flood with spoofed sources | One row for each local port and minute | Small |

Set the bucket quota (`max_size_gib`) from these flow estimates. After the first hosts run in Phase 1, add the measured sizes of the `sni`, `nat-ranges` and `proxy-access` logs. See [Cargo log bucket ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md).

Flow rows hold remote addresses, and can be personal data. Keep them in the region. The AUP and the privacy policy must state the collection before an operator turns on `is_abuse_logs_enabled`.

## NAT port ranges

The NAT port ranges are a data plane change. Phase 1 changes live NAT on every host.

Today a VM without a dedicated public IPv4 address goes through two NAT steps:

1. Inside the VM namespace, Metal masquerades the guest to the namespace address (`setMasquerade` in [nat.go](../../metal/internal/network/nat.go)).
2. On the host, a rule masquerades `10.0.0.0/8` to the uplink. [install-metald.sh](../../atlas/scripts/install-metald.sh) adds it on each host. [net-setup.sh](../../metal/internal/vm/scripts/net-setup.sh) has the same rule.

A VM with a dedicated public IPv4 address gets an SNAT rule for its namespace address, inserted first in `POSTROUTING` (`publicIPv4Steps` in [nat.go](../../metal/internal/network/nat.go)). A dedicated public IPv6 address has its own SNAT rule (`publicIPv6Steps`).

The host MASQUERADE picks any source port, so the shared address and a port do not name a VM. The change:

- Metal gives each VM on a host a fixed source port range on the shared host address. This follows [RFC 7422](https://www.rfc-editor.org/rfc/rfc7422).
- A host rule `SNAT --to-source <shared address>:<first>-<last>` for the namespace address of the VM comes before the `10.0.0.0/8` masquerade.
- The VM slots of the host share the ports 1024 to 65535. The size of each range sets the slot count of the host. The range size is pending host capacity.
- metald stores a NAT slot, from 0 to the slot count minus 1, in each VM record. The range of the VM comes from its slot. A metald patch gives a slot to each existing VM.
- The range cannot come from the metald user ID. The user ID range has 65,536 values ([user_id.go](../../metal/internal/vm/user_id.go)), and the ports cannot give a range to each of them.
- A VM with a dedicated public IPv4 address gets no slot and no `nat-ranges` rows. Its SNAT rule stays first, so it wins over the range rules.
- IPv6 does not use the shared IPv4 address or a range. Attribution of an IPv6 address uses the Public IP history in Central.

Placement rule, with the range size:

- When the host has no free slot, Metal refuses to set up the VM network, with an error.
- Atlas placement must not put more VMs on a host than its slot count.

Switch:

- Metal uses the ranges only when `metald.toml` has `[nat] is_port_range_enabled = true`. The default is false. Atlas writes the value from the Atlas Settings check `is_nat_port_range_enabled`.
- When the value is false, Metal keeps the `10.0.0.0/8` MASQUERADE, adds no range rules and writes no `nat-ranges` rows.
- When an operator turns it on, the new flows of a VM use its range. Existing connections keep their conntrack mapping until they end.
- Rollback: turn the switch off. Metal removes the range rules, and the MASQUERADE handles new flows.
- Metal never changes the range size in place. A new range size needs a drained host.

The `nat-ranges` log:

- `assigned`: Metal writes a row when it sets up the VM network on the host, at create, start and migration in.
- `freed`: Metal writes a row when it removes the VM network from the host, at delete, stop and migration out.
- A migrated VM gets a new range on its new host.
- Each row holds the time, the host, the shared address, the first port, the last port, the VM and the event.
- Metal also writes the full table once a day.
- Metal uploads it like the other logs. See [log upload](contract.md#log-upload). The retention is 180 days.
- The log holds no customer traffic, so it does not need the `[abuse] logs` switch.

Attribution: a complaint about a shared address names an address, a port and a time. Attribution uses the `nat-ranges` log only. The flow log holds the guest address and the guest port, before NAT. The public port of a complaint is only in `nat-ranges`, so flow rows cannot be joined to a complaint by port. See [Watcher attribution ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#attribution).

Limit: a VM that uses all ports of its range cannot open more flows to the same remote address and port at the same time.

## Baseline script

Until the per-VM metrics are in Datum, [atlas-abuse-baseline.py](../../scripts/atlas-abuse-baseline.py) gives per-VM rates from the 10 s metric samples that Metal keeps today. An operator runs it on every Metal host from 2 to 7 November 2026. Agents do not run it. How Watcher uses the result is in [Watcher calibration ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#calibration).

The samples include mesh traffic and count a SYN-ACK as a SYN, so the script gives upper bounds only.

```sh
# On each Metal host, as root. It only reads, and writes one JSON file to /tmp.
sudo nice -n 19 ionice -c3 python3 -I atlas-abuse-baseline.py collect
# Anywhere, on the collected files.
python3 -I atlas-abuse-baseline.py merge abuse-baseline-*.json --csv fleet.csv
```

::: details Code to change

- [Traffic counters](../../metal/internal/network/traffic/bpf/track_traffic.c) count a SYN-ACK as a SYN today. Add the new counters and maps here, and read them in the [packet monitor](../../metal/internal/network/traffic/monitor.go).
- The [metald config](../../metal/cmd/metald/config.go) reads `[telemetry] abuse_metrics`, `[abuse] logs`, `[abuse] flow_map_entries`, `[abuse] flow_entries_per_vm` and `[nat] is_port_range_enabled`.
- Flow map: add the LRU flow map, the sketch map and the per-VM counter map to [track_traffic.c](../../metal/internal/network/traffic/bpf/track_traffic.c). Both TCX programs update the flow map, and apply the per-VM cap when they create an entry. The [packet monitor](../../metal/internal/network/traffic/monitor.go) reads the map each minute, writes the change of each changed entry and the summary rows to the `flow` log, and deletes the entries with no packet for 2 min.
- SNAT ranges: the network allocator ([nat.go](../../metal/internal/network/nat.go), next to `publicIPv4Steps`) renders the range rule of each VM before the host masquerade from [install-metald.sh](../../atlas/scripts/install-metald.sh) and [net-setup.sh](../../metal/internal/vm/scripts/net-setup.sh), when `[nat] is_port_range_enabled` is true. iptables accepts a port range only with a protocol match such as `-p tcp` or `-p udp`, so each VM gets one rule for each protocol.
- NAT slot: add the slot to the VM record in [records.go](../../metal/internal/vm/records.go), next to the user ID from [user_id.go](../../metal/internal/vm/user_id.go). Add a metald patch that gives a slot to each existing VM. Return an error when the host has no free slot.
- `nat-ranges` writer: Metal writes a row when it sets up or removes the VM network on the host, and the full table once a day.
- [System telemetry](../../metal/internal/telemetry/system/system.go) skips VM interfaces. The `vm_abuse_*` exporter is separate from it.
- The [telemetry registry](../../metal/internal/telemetry/registry.go) rejects samples with a label in `churningLabels`. Exempt metrics with the `vm_abuse_` prefix from this check. All other metrics keep the check.

:::
