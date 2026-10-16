# Mesh traffic loss after a WireGuard restart

| Field          | Value                                                                  |
| -------------- | ---------------------------------------------------------------------- |
| Date           | 2026-10-16                                                             |
| Environment    | Production, region Mumbai                                              |
| Component      | `atlas/scripts/configure-wireguard.sh`, WG Mesh                        |
| Severity       | Medium: partial mesh outage                                            |
| Status         | Fixed                                                                  |
| Affected up to | `8b0ea21ec2a98958aa274881dbfe7465cd7c26bc` (2026-10-16, `develop`)     |

## Summary

Operators raised the private network MTU in Mumbai to 9000 for faster WireGuard traffic between hosts. They then ran **Configure WireGuard** on each Metal Server to set the new `wg0` MTU. On four of the six hosts, the script restarted `wg0`. The restart removed the WG Mesh program from `wg0`, and nothing attached it again. For about 7 minutes, VMs on those hosts did not receive mesh traffic from other hosts. A `metald` restart attached the program again.

## Impact

| Period (UTC)    | Effect                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------ |
| 18:06 - 18:14   | 18 VMs on four hosts did not receive mesh traffic from other hosts. This includes HTTP proxy traffic to those VMs from a proxy on another host. |

Traffic between VMs on the same host continued. VMs on the affected hosts could still send. No data was lost.

## Detection

A check after the change found no `handle_wireguard_packet` program on `wg0` on four hosts. On those hosts, the kernel counter `Ip6InUnknownProtos` increased by up to 67 packets each second. WireGuard peers and handshakes were healthy, so the WireGuard status did not show the fault.

## Timeline (UTC)

| Time            | Event                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------ |
| 17:51 - 18:03   | The bond hash on all six hosts changes to `layer3+4`.                                      |
| 18:03 - 18:06   | The bond MTU on all six hosts changes to 9000. A jumbo ping passes between all host pairs. |
| 18:06 - 18:08   | **Configure WireGuard** runs from Desk. On four hosts, `wg-quick@wg0` restarts with MTU 8940. On two hosts, `wg0.conf` does not change. |
| 18:08 - 18:09   | Host sync adds the host peers and routes again. The mesh program stays detached.          |
| 18:10           | A check finds the missing program and the increasing `Ip6InUnknownProtos` counter.         |
| 18:14:20 - 18:14:42 | `metal` restarts on the four hosts. Each start attaches the program again.             |
| 18:15           | A check shows the program on all hosts and no new unknown-protocol drops.                  |

## Root cause

`configure-wireguard.sh` restarts `wg-quick@wg0` when the `[Interface]` part of `wg0.conf` changes. The script computes the `wg0` MTU from the uplink MTU, so the new uplink MTU changed that part.

The restart deletes `wg0` and creates it again. The WG Mesh tc program `handle_wireguard_packet` is attached to the `wg0` device, so the delete removed it. Only `metald` attaches this program, through `atlas-wg-mesh configure`, and it does this only when it starts.

A host sends a VM packet to another host as IPv6-in-IPv6 to the `wg0` address of the destination host. The program on the destination `wg0` removes the outer header and delivers the packet to the VM. Without the program, the kernel received a packet with next header 41 and had no tunnel device for it, so it dropped the packet.

### Contributing factors

- The script comment noted that a restart drops the host peers. It did not note that the restart also removes the mesh program.
- Host sync restored the peers and routes in about one minute. The WireGuard status then looked normal.
- A live MTU change (`ip link set wg0 mtu`) would have avoided the restart. The procedure did not state that.

## Resolution

- `metal` was restarted on the four hosts. This attached the program again.
- On the other two hosts, `wg0` was set to MTU 8940 live, and `wg0.conf` was updated to match.
- `configure-wireguard.sh` now restarts `metald` after it restarts `wg0`, so `metald` attaches the program again.

## Lessons

- A program that is attached to an interface does not survive a restart of that interface. The code that restarts the interface must attach the program again.
- Healthy WireGuard peers do not prove that mesh traffic arrives. After a change to `wg0`, check the program on `wg0` and the `Ip6InUnknownProtos` counter.
- Apply an MTU change to a running interface live, and update the configuration file to match.
