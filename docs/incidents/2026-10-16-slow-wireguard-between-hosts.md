# Slow WireGuard throughput between hosts

| Field          | Value                                                                  |
| -------------- | ---------------------------------------------------------------------- |
| Date           | 2026-10-16                                                             |
| Environment    | Production, region Mumbai                                              |
| Component      | WG Mesh, host network setup                                            |
| Severity       | Low: performance                                                       |
| Status         | Mitigated                                                              |
| Affected up to | `8b0ea21ec2a98958aa274881dbfe7465cd7c26bc` (2026-10-16, `develop`)     |

## Summary

`wg0` between two Mumbai hosts carried only 1.8 Gbit/s. The private network under it carried 23 Gbit/s. Two host settings caused most of the gap: the 1500-byte MTU and the `layer2` bond hash. Jumbo frames and the `layer3+4` hash raised `wg0` to about 8 to 9.5 Gbit/s and the private network to 35 to 37 Gbit/s. One WireGuard tunnel cannot go much higher on this hardware.

## Impact

No traffic was lost. Host-to-host traffic on `wg0` was slow. For this reason, VM migration uses the private network with mutual TLS, not `wg0`. VMs were not affected, because their throughput limit is far below the `wg0` rate.

## Detection

An operator compared `wg0` with the private network and reported the gap.

## Root cause

Each pair of hosts has one WireGuard tunnel. A tunnel is one UDP flow, so the NIC puts all its packets on one receive queue and one CPU core. That core does the NIC receive, the UDP stack, and the WireGuard handoff for every packet. It reached about 166,000 packets each second at 97% softirq time. At the 1440-byte `wg0` MTU, that is 1.8 Gbit/s. Plain TCP on the private network costs much less per byte, because the NIC merges TCP packets (hardware GRO).

A perf profile of that core showed:

| Share | Work                                                                                     |
| ----- | ---------------------------------------------------------------------------------------- |
| 30%   | Wakeups of WireGuard crypto workers. WireGuard sends each packet to the next CPU in turn, across all 128 CPUs. |
| 20%   | Header copies, because the `bnxt_en` NIC splits headers from data (header-data split).  |
| 10%   | UDP and conntrack                                                                        |
| 1%    | WG Mesh BPF programs                                                                     |

The bond used the `layer2` transmit hash. All traffic from one host to another host has the same MAC pair, so it used one 25G link of the 2 x 25G bond. Public egress uses the MAC address of the gateway, so it used one link as well.

## What did not help

| Test                                              | Result                                                         |
| ------------------------------------------------- | -------------------------------------------------------------- |
| Turn off header-data split                        | `bnxt_en` rejects it at MTU 9000.                               |
| Turn off the deepest CPU idle state (C2)          | No change (9.46 and 12.0 Gbit/s).                              |
| Two tunnels between the same hosts                | 12.2 Gbit/s in total, not 18.                                  |

With two tunnels, no core was saturated. Crypto jobs that WireGuard sent to the busy receive cores waited 2 to 3 ms on average, compared with 14 µs elsewhere. WireGuard delivers the packets of a tunnel in order, so one late job stops the tunnel. The [Netdev 0x18 paper on WireGuard multi-core scaling](https://netdevconf.info/0x18/docs/netdev-0x18-paper23-talk-paper.pdf) reports the same limit of 12 to 14 Gbit/s for several tunnels.

## Resolution

All six Mumbai hosts now use:

- MTU 9000 on `bond0` and `bond0.2`, with the default route set to MTU 1500.
- MTU 8940 on `wg0`.
- The `layer3+4` bond transmit hash.

| From host A to host B         | Before        | After          |
| ----------------------------- | ------------- | -------------- |
| `wg0`, 1 stream               | 1.8 Gbit/s    | 8.0 Gbit/s     |
| Private network, 8 streams    | 23.4 Gbit/s   | 35.3 Gbit/s    |

During the rollout, a WireGuard restart detached the WG Mesh program on four hosts. See [Mesh traffic loss after a WireGuard restart](2026-10-16-wireguard-restart-detached-mesh.md).

## Lessons

- One WireGuard tunnel uses one receive core. Measure packets each second on that core, not only the link rate.
- Check the bond transmit hash on a new host. `layer2` uses one link for all traffic to one destination.
- Use jumbo frames on the private network when the provider supports them.
- Send bulk traffic between hosts over TLS on the private network, as VM migration does. Kernel WireGuard gives about 8 to 9.5 Gbit/s for each pair of hosts on this hardware.
