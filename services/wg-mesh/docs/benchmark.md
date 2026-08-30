# Atlas WG Mesh benchmarks

Bare metal. 1 GbE uplink. WireGuard MTU `1420`. VM interface MTU `1380`.

## Throughput

Single TCP stream. Debug disabled.

| Path | Throughput | Line rate |
| --- | ---: | ---: |
| Raw 1 GbE | 941 Mbit/s | 100% |
| Host-to-host WireGuard | 885 Mbit/s | 94% |
| Atlas VM-to-VM | 847 Mbit/s | 90% |

Atlas delivers 95% of WireGuard throughput. The main cost is the 40-byte tunnel header and smaller VM MTU. BPF encapsulation and decapsulation add little overhead.

| Path | Single stream | Four streams |
| --- | ---: | ---: |
| WireGuard | 885 Mbit/s | 885 Mbit/s |
| Atlas | 847 Mbit/s | 844 Mbit/s |

One stream saturates the 1 GbE link. More streams do not help. On faster links, throughput should track WireGuard until tunnel encryption becomes the limit.

## Packet rate

With 200-byte UDP packets, Atlas reached about 230,000 packets/s and 322 Mbit/s. This is the worst case for per-packet BPF work.

## Debug mode

Measured on an 8-core host over 12 seconds. One busy core equals 1,200 jiffies.

| Load | Mode | Throughput | Extra CPU |
| --- | --- | ---: | --- |
| TCP, about 77,000 packets/s | Debug disabled | 847 Mbit/s | Baseline |
| TCP, about 77,000 packets/s | Debug enabled, no reader | 847 Mbit/s | No measurable change |
| TCP, about 77,000 packets/s | `debug dump` draining | 847 Mbit/s | About 0.33 core, 30% more busy time |
| UDP, about 230,000 packets/s | `debug dump` draining | 322 Mbit/s | About 0.88 core, 52% more busy time |

Debug enabled without a reader had no measurable cost. `debug dump` uses one userspace reader thread. Its cost is single-core scale: about one-third of a core at normal TCP load and almost one core at high packet rates.

## Discovery rate limiting

The limiter runs only on remote-cache misses. It had no measurable effect on established-flow throughput at default, custom, or disabled settings.
