# Abuse metrics

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Metal counts the traffic of each VM and exports the counts to Datum as metrics. Metal also writes the SNI log and the NAT log, and uploads them to the Cargo log bucket. Metal does not compare a count with a threshold, and it does not make a finding.

Watcher, a Cargo module in each region, reads the metrics and the logs, finds abuse and sends findings to Central. Its inputs are in [Watcher inputs ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#inputs). Finding codes, thresholds, windows and feeds are in [Watcher finding codes ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#finding-codes). Profiles are in [Central profiles ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#profiles).

Metal never reads the content of guest traffic. It reads only the TLS ClientHello and, for the [stratum match](#stratum-match), the first payload bytes of a new flow. The acceptable use policy (AUP) and the privacy policy must say so.

## Counters

A new TCX program and new maps sit next to [track_traffic.c](../../metal/internal/network/traffic/bpf/track_traffic.c). The maps belong to Metal, so they need no WG Mesh map layout patch.

Metal counts only the traffic that the guest starts. It never counts a reply as a new flow. Today `track_traffic.c` counts a SYN-ACK as a SYN. The new `tcp_initiated` counter counts a SYN without ACK.

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
| `vm_abuse_public_address` | Gauge | `address` | 1 for each public address that the VM has for itself |

Labels:

- `scope` is `public` or `mesh`. A `mesh` destination is in the same prefix list as the [`tc` policer](../networking/host-networking.md#throughput-limits): RFC 1918, `fc00::/7`, `100.64.0.0/10` and the platform ranges. All other destinations are `public`.
- `protocol` is `tcp`, `udp`, `icmp` or `other`.
- `port_class` is a name from [port classes](#port-classes). On `vm_abuse_destinations`, it can also be `all`: every public destination, on any port.
- `window` is `60s` or `10m`.
- `direction` is `transmit` or `receive`. `transmit` counts public UDP bytes that the VM sends from a source port in the `amplification` class. `receive` counts public UDP bytes that the VM receives to a destination port in that class.
- `port` is the port number from the `amplification` class. Watcher uses it to name the protocol.
- `address` is a public IPv4 or IPv6 address that the VM has for itself. It is never the shared host MASQUERADE address. Watcher checks these addresses against DNSBLs and shared abuse databases. See [Watcher feeds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#feeds).

Maps:

- A flow table (LRU) for each VM, keyed by destination address, port and protocol. Each entry holds the first-seen time and an answered flag. It holds about 16k entries, 768 KiB for each VM. The answered, first-packet and unanswered counts use it.
- HyperLogLog registers for each port class and window give the distinct destinations.
- The `_max` gauges read the flow table. They report only the busiest destination, not the address.

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

SNI rows can be personal data. Keep them in the region. The AUP and the privacy policy must state the collection before the SNI log is switched on.

Metal uploads the log to the Cargo log bucket every 5 min. See [log upload](contract.md#log-upload). Watcher reads it with the bucket read key. See [Watcher inputs ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#inputs).

## NAT log

The NAT log has one row for each conntrack `NEW` and `DESTROY` event of a VM behind the host MASQUERADE: VM, protocol, inside address and port, outside address and port, destination address and port, event and time.

A complaint about a shared IPv4 address names only the address, the port and the time. The NAT log finds the VM.

A VM with a dedicated public address does not go through the host MASQUERADE, so its flows are not in the NAT log. Thus the Watcher C2 and Tor feed matches cover only VMs behind shared NAT. See [Watcher feeds ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#feeds).

Metal uploads the log to the Cargo log bucket every 5 min. See [log upload](contract.md#log-upload). Watcher reads it with the bucket read key. Watcher checks the public source addresses against shared abuse databases, and finds the VM for an address, a port and a time. See [Watcher attribution ↗](https://github.com/frappe/cargo/blob/develop/docs/watcher.md#attribution).

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

- [Traffic counters](../../metal/internal/network/traffic/bpf/track_traffic.c) count a SYN-ACK as a SYN today.
- [System telemetry](../../metal/internal/telemetry/system/system.go) skips VM interfaces. The `vm_abuse_*` exporter is separate from it.
- The [telemetry registry](../../metal/internal/telemetry/registry.go) rejects samples with a label in `churningLabels`. Exempt metrics with the `vm_abuse_` prefix from this check. All other metrics keep the check.

:::
