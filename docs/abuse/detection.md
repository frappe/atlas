# Abuse detection

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

Metal counts guest traffic on each host and compares the rates with thresholds. A match is a finding. Metal reports each finding to Atlas as an Abuse Signal in [host sync](../region/host-sync.md).

Atlas never reads the content of guest traffic. It reads only the TLS server name (SNI) and, for mining, the first payload bytes of new flows to non-standard ports. The acceptable use policy (AUP) and the privacy policy must say so.

## Counters

A new TCX program and new maps sit next to [track_traffic.c](../../metal/internal/network/traffic/bpf/track_traffic.c). The maps belong to Metal, so they need no WG Mesh map layout patch.

Metal splits each counter into public and private traffic. Private destinations use the same prefix list as the [`tc` policer](../networking/host-networking.md#throughput-limits): RFC 1918, `fc00::/7`, `100.64.0.0/10` and the platform ranges.

Counters for each VM:

- Sent and received packets and bytes.
- `tcp_initiated`: a SYN without ACK. Metal keeps `tcp_syn_packets`, and adds the new counter as an optional `NetworkUsage` field.
- SYN-ACK, RST and ICMP unreachable received.
- UDP and ICMP packets in each direction.
- A flow LRU keyed by destination address, port and protocol, with the first-seen time and an answered flag. It holds about 16k entries, 768 KiB for each VM.
- Distinct destinations for each port class, in HyperLogLog registers.
- Port class counters. The lists are in Abuse Settings. Metal only counts these ports. It does not block them.
- `udp_first_packets`: UDP to a destination that is not in the flow LRU.
- Stratum match: the first payload bytes of a new flow to a non-standard port contain `mining.subscribe` or `login`.
- CPU use against the quota, from `runtime.GetUsage`.

Starting port classes:

| Class | Ports |
| --- | --- |
| `auth` | 21, 22, 23, 445, 1433, 3306, 3389, 5432, 5900, 6379, 27017 |
| `smtp` | 25 |
| `submission` | 465, 587 |
| `amplification` | 53, 123, 161, 389, 1900, 11211 |
| `mining` | 3333, 4444, 5555, 7777, 14444. An operator can change the list. |

Counters reset when metald restarts. The detector treats a reset as a gap, not as a rate.

## SNI log

The SNI log has one row for each outbound TLS connection: VM, destination address and port, server name and time. An eBPF program reads only the ClientHello of a new outbound TCP flow. It also reads the ClientHello after STARTTLS on ports 25 and 587. It reads no other payload.

Metal analyses the rows as a stream for each VM, with fixed memory. Metal uploads the raw rows to the log bucket. See [logs](data.md#logs).

Limits:

- No server name for traffic that is not TLS, or for Encrypted Client Hello.
- No detection of MX lookups, generated domain names (DGA), DNS tunnels or data exfiltration by volume.

## Findings

A finding code uses the GuardDuty style `<threat>:<resource>/<name>`. A finding never names a reporter. Customer evidence holds counts, ports, times and destination prefixes truncated to `/24`.

| Finding code | Signal | Source | Automatic action |
| --- | --- | --- | --- |
| `DoS:VM/OutboundFlood` | Public pps or bps above the threshold | Metal | Breaker: policer |
| `DoS:VM/SynFlood` | Initiated SYN to few destinations, few SYN-ACK | Metal | Breaker |
| `Recon:VM/Portscan` | Fan-out with a low answer ratio | Metal | Breaker on the port |
| `UnauthorizedAccess:VM/BruteForce` | Auth ports to many addresses, or many connections to one | Metal | Breaker on the port |
| `Smtp:VM/BlockedPort25` | A few blocked port 25 attempts | Metal | None. Port 25 is already blocked. |
| `Spam:VM/Smtp` | Many port 25 attempts, a submission spray, or our address on a DNSBL or in SNDS | Metal, feed | None |
| `Reflector:VM/<protocol>` | Amplification ratio, or a Shadowserver report | Metal, feed | Breaker on the source port |
| `CryptoCurrency:VM/PoolConnection` | Pool address from the feed, or a stratum match | Metal | Default `mining_pools` block |
| `CryptoCurrency:VM/PoolName` | Pool name in the SNI | Metal | None |
| `CryptoCurrency:VM/SustainedCpu` | CPU above 95% of the quota for 6 h | Metal | None. Severity input only. |
| `Backdoor:VM/C2Activity` | Flows to Spamhaus DROP or abuse.ch Feodo addresses | Metal | None |
| `Backdoor:VM/Beaconing` | Connections to one name at regular intervals | Metal, SNI | None |
| `Phishing:VM/LookalikeDomain` | Brand terms or lookalike names in the SNI | Metal, SNI | None |
| `Spam:VM/MailVolume` | Connections to submission names far above the baseline | Metal, SNI | None |
| `Proxy:VM/Tor` | Connections to Tor relays from the public consensus | Metal | None |
| `Phishing:Site/Reported` | Hostname on Safe Browsing, OpenPhish or URLhaus, or in a report | Proxy, feed | None |
| `Content:Site/Illegal` | Report only | Report | None |
| `DoS:VM/InboundFlood` | Received pps or bps on a VM or an address | Metal, proxy | Receive policer |

Central decides the next step for each finding. See the [Central policy engine ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## Threshold rules

1. Every rule has a volume floor and a shape condition: answer ratio, packet size, fan-out or protocol.
2. Third-party harm rules count public traffic only. Mesh traffic is a host safety matter.
3. Third-party harm limits are absolute. They do not change with the VM size.
4. Host safety limits are relative to the host capacity.
5. Traffic to the tenant's own addresses and domains never counts.

Capacity basis:

| Fact | Value | Source |
| --- | --- | --- |
| Highest pps from one VM | About 100k to 113k, 200-byte UDP | [WG Mesh benchmarks](../networking/wg-mesh/benchmarks.md) |
| One WireGuard receive core | About 166k pps at 97% softirq | [Slow WireGuard incident](../incidents/2026-10-16-slow-wireguard-between-hosts.md) |
| Byte limit for each VM | 500 MiB/s, no pps limit | Central settings |
| Host uplink | 2 x 25G bond, MTU 9000 | [WG Mesh benchmarks](../networking/wg-mesh/benchmarks.md) |
| Highest host load seen in BOM | 88k pps on `wg0`, 67% softirq on one core | [Fleet data](#fleet-data) |

## Windows

| Window | Use |
| --- | --- |
| 1 s map reads, 10 s evaluation, 3 in a row | Floods, host safety |
| 60 s sliding | Scans, SYN floods, reflectors |
| 10 min sliding | Brute force, SMTP |
| 1 h | Slow scans |
| 6 h | CPU |

Breaker release and repeat rules are in [circuit breakers](enforcement.md#circuit-breakers).

## Thresholds

These are starting values. No value is active until [calibration](#calibration) confirms it.

Outbound to public destinations:

| Rule | Trigger | Breaker |
| --- | --- | --- |
| UDP flood | UDP above 20k pps for 30 s, and received below 10% of sent or fewer than 8 destinations | 1k pps public UDP |
| Small-packet flood | Above 20k pps for 30 s, average packet below 200 bytes | 2k pps public |
| Volumetric | Above 1 Gbit/s for 60 s, received below 5% of sent | 100 Mbit/s public |
| ICMP flood | Above 1k pps for 30 s | 100 pps ICMP |
| SYN flood | Initiated SYN above 2k/s for 30 s, SYN-ACK below 10% | Drop new TCP to the top destinations |
| Horizontal scan | 256 or more addresses on one port in 60 s, below 25% answered | Drop new flows to that port |
| Vertical scan | 512 or more ports on 4 or fewer addresses in 60 s, below 25% answered | Drop new flows to those addresses |
| Slow scan | 2,000 or more unanswered address and port pairs in 1 h | None. Low signal. |
| Brute force | Auth ports to 64 or more addresses in 10 min, or 300 or more connections to one address | Drop new flows to that port |
| Reflector | Amplification source port, bytes out above 10 times bytes in, above 10 Mbit/s for 60 s | Drop outbound from that port |

SMTP:

| Rule | Trigger | Result |
| --- | --- | --- |
| Port 25 misconfiguration | 1 to 19 blocked attempts in 10 min | `Smtp:VM/BlockedPort25`, Low, always sent to Central |
| Port 25 spam | 500 or more blocked attempts in 10 min, or 50 or more destinations | `Spam:VM/Smtp`, Medium |
| Submission spray | Ports 465 and 587 to 20 or more addresses, or 300 or more connections, in 10 min | `Spam:VM/Smtp`, Medium. No breaker. |
| DNSBL listing | Any listing of an address that Atlas gave to a VM | `Spam:VM/Smtp` from the feed |

Other rules:

| Rule | Trigger |
| --- | --- |
| Mining pool, feed | 1 attempt |
| Mining pool, stratum | 1 matched flow open for 10 min |
| C2 | 1 flow to abuse.ch Feodo, or 3 flows in 1 h to Spamhaus DROP |
| Inbound flood | Above 20k pps or 1 Gbit/s for 30 s: a signal for a notice. Above 80k pps, or uplink above 60%: [inbound DDoS steps](enforcement.md#inbound-ddos). |

Host safety rules count any destination, the mesh included. A host safety trigger is not an Abuse Signal. It opens no case and sends no event to Central.

| Rule | Trigger | Action |
| --- | --- | --- |
| One VM | Above 80k pps for 30 s | Policer to 50k pps |
| One host | Above 120k pps toward `wg0`, or softirq above 80% on a WireGuard core | Policer on the top VMs |

Metal reports each trigger, and its policer and breaker counts, as telemetry metrics with the `metal_abuse_` prefix, for example `metal_abuse_host_safety_triggers_total` and `metal_abuse_breakers_active`. An Insights alert on these metrics posts to Raven `#abuse`. See [telemetry](contract.md#telemetry).

HTTP proxy, for each client address and site: `limit_req` 100 requests a second with a burst of 500, and `limit_conn` 200. The values are high on purpose, because Indian mobile networks put many users behind one CGNAT address. A site above 10 times its 7-day p99 for 5 min alerts an operator.

## Profiles

Central sets `abuse_profile` on each VM, so Atlas needs no team data. How Central picks a profile is in the [Central abuse profile ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#abuse-profile). Atlas sets `infrastructure` on privileged tenant-0 VMs. Central never sets it.

| Profile | Effect |
| --- | --- |
| `strict` | Third-party harm triggers at 0.5 times. Breaker durations are doubled. |
| `standard` | The values above |
| `relaxed` | Third-party harm triggers at 2 times. Host safety is unchanged. |
| `infrastructure` | Host safety rules only. Atlas sends no signal to Central. |

The multipliers are in Abuse Settings. An empty `abuse_profile` means `standard`, so old VMs keep working.

## Overrides

Central approves an Abuse Exception for one server and one finding code. Central sends it to Atlas as a VM Abuse Override row on the VM: finding code, multiplier or disabled, expiry and the exception reference. Metal applies it to that VM only, until the expiry. The exception rules, such as the longest period, are in the [Central abuse exception ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#abuse-exception).

Host safety rules and the default blocks take no override. The only exception to a default block is `is_port_25_allowed`. See [default blocks](enforcement.md#default-blocks).

## Feeds

Atlas pulls feeds and writes each match as an Abuse Signal with source Feed. A feed is not an abuse report, so Atlas sends no reply.

| Feed | Use |
| --- | --- |
| Mining pools | Pool address set for the `mining_pools` default block. Refreshed every day. |
| DNSBLs: Spamhaus ZEN, Barracuda and others | Atlas checks every public IPv4 address in the pools every hour. A listing is a signal against the holder, or a flag on a free address. |
| Microsoft SNDS | Spam trap hits and complaints for our address ranges |
| Shadowserver | Free reports for the region's prefixes, through its API |
| Spamhaus DROP, abuse.ch Feodo | C2 addresses |
| Tor consensus | Tor relay addresses |
| Safe Browsing, OpenPhish, URLhaus | Phishing and malware hostnames for proxy sites. See [site tombstone](enforcement.md#site-tombstone). |

A released address that had a case moves to `Blocked` for a 30-day cool-down. A new optional `blocked_until` field on Public IP Allocation holds the end, and a job unblocks the address. See [block an address](../networking/public-ips.md#block-an-address).

## Fleet data

The Insights dashboard [Abuse Protection ↗](https://dashboard.frappe.io/insights/dashboards/8eplc8khne) on dashboard.frappe.io (workbook "Abuse Protection") shows:

- Host packet load and peaks on `wg0` and the uplink for 14 days, from regional telemetry.
- Softirq on the busiest core, for 2 days only. A 14-day scan for each CPU took about 60 s and slowed the Atlas alert queries.
- Live servers by abuse profile, from Central.

Telemetry does not have these values, so the baseline script is still necessary:

- Traffic for each VM. metald skips VM interfaces in [system telemetry](../../metal/internal/telemetry/system/system.go).
- Proxy rates for each site or client. Vector sends counts for each node and status class only.

BOM, 16 to 29 October 2026. Highest 1-minute rate in each 5-minute bucket:

| Host | `wg0` receive, max (p99) | `wg0` transmit, max | Busiest core softirq, max (p99) | Uplink receive, max |
| --- | --- | --- | --- | --- |
| metal-bom-1 | 80k (66k) | 88k | 59% (41%) | 87k |
| metal-bom-2 | 74k (43k) | 84k | 34% (20%) | 601k |
| metal-bom-3 | 82k (55) | 15k | 67% (4%) | 1.79M |
| metal-bom-4 | 85k (52k) | 79k | 49% (32%) | 84k |
| metal-bom-5 | 728 (64) | 1.7k | 11% (1%) | 61k |
| metal-bom-6 | 5.3k (134) | 3.2k | 9% (1%) | 45k |
| metal-bom-7, from 26 October | 119 (14) | 450 | 26% (3%) | 72k |

- The host safety triggers have 30% to 40% headroom. Neither trigger would have fired.
- One-minute rates hide shorter bursts. Compare them with breaker events in shadow mode.
- metal-bom-1 runs near its peak all the time.
- The uplink spikes on metal-bom-3 and metal-bom-2 have no known cause, flood or transfer. Find the cause before you set inbound flood thresholds.

Servers by profile, 29 October 2026:

| Profile | Reason | Teams | Servers |
| --- | --- | --- | --- |
| `relaxed` | Trust tier Elite, manual override | 1 | 30 |
| `strict` | Free trial server | 12 | 12 |
| `strict` | Team never paid | 9 | 16 |
| `standard` | None | 0 | 0 |

No invoice exists yet, so `standard` stays empty until billing starts.

## Calibration

1. Baseline. Host load is in [fleet data](#fleet-data). For rates of each VM, an operator runs [atlas-abuse-baseline.py](../../scripts/atlas-abuse-baseline.py) on every Metal host from 2 to 7 November 2026, so that the 7 days of samples include the October month end. Agents do not run it. The data includes mesh traffic and counts a SYN-ACK as a SYN, so it gives upper bounds only.
2. Shadow. Every Abuse Threshold `mode` is Shadow for at least 4 weeks, with a month end. Signals get `is_shadow`. Atlas sends no shadow signal to Central.
3. Red team in staging, from a test tenant. Each row must fire in time:

   | Tool | Rule | Fires within |
   | --- | --- | --- |
   | masscan at 100, 1k and 10k pps | Horizontal scan | 60 s |
   | nmap `-T3` and `-T4`, top 1000 ports | Vertical scan | 60 s |
   | hping3 `--flood`, SYN and UDP | SYN flood, UDP flood | 30 s |
   | iperf3 UDP at 2 Gbit/s to a silent sink | Volumetric | 60 s |
   | xmrig to a test stratum server | Mining, stratum | 10 min |
   | swaks loop to 100 MX hosts on port 25 | Port 25 spam | 10 min |

   These must stay silent: a bench build (yarn, pip, git), `apt upgrade`, a 50 GB backup to S3, a month-end run of 10k emails, a 500 Mbit/s WireGuard VPN and a crawler on 1k sites.
4. Active. Switch one finding code at a time, when false positives are below 1 for each 1,000 VM-days and every red-team row fires.

After that, operators mark disputed signals `is_false_positive`. A weekly digest shows false positives for each finding code. Operators tune each region in Abuse Settings.

The baseline script:

```sh
# On each Metal host, as root. It only reads, and writes one JSON file to /tmp.
sudo nice -n 19 ionice -c3 python3 -I atlas-abuse-baseline.py collect
# Anywhere, on the collected files.
python3 -I atlas-abuse-baseline.py merge abuse-baseline-*.json --csv fleet.csv
```
