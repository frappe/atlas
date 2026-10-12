# Host network loss from allmulticast on Broadcom NICs

| Field          | Value                                                                                |
| -------------- | ------------------------------------------------------------------------------------ |
| Date           | 2026-10-10                                                                           |
| Environment    | Production, region Mumbai                                                            |
| Component      | WG Mesh (`services/wg-mesh/cli`), host setup                                         |
| Severity       | High: region outage                                                                  |
| Status         | Fixed                                                                                |
| Affected up to | `6ab904c076df08debacb8018afac474d02a7ef21` (2026-10-10, `develop`)                   |

## Summary

The Mumbai region moved to a new bare-metal host with Broadcom `bnxt_en` NICs in an LACP bond. Each time metald started, the host lost all network traffic within seconds, and only a reboot or console access restored it. WG Mesh set the private uplink to allmulticast. On this NIC and firmware, allmulticast without promiscuous mode stops all receive traffic on the bond. The fix is NIC firmware `230.0.1.0/pkg 230.1.116.0` or later.

## Impact

| Period (UTC)  | Effect                                                                                       |
| ------------- | -------------------------------------------------------------------------------------------- |
| 15:10 - 17:28 | Atlas could not manage the region.                                                           |
| 17:28 - 22:45 | All VMs in the region were stopped or unreachable.                                           |
| 18:15 - 20:55 | The host lost its network three times. Each time needed console access or a reboot.          |
| 22:45 - 22:55 | VMs ran, but the HTTP proxy could not reach tenant VMs. Custom domains did not respond.      |
| 17:28 - 22:43 | Image cleanup failed 739 times, because the object storage VM was down.                      |

No data was lost.

## Detection

Operators saw the faults directly. SSH to the host timed out, and the console showed that the gateway neighbor entry was `INCOMPLETE`. A packet capture on the bond showed only outgoing ARP requests and no incoming frames. The LACP partner MAC address changed to `00:00:00:00:00:00`.

## Timeline (UTC)

| Time        | Event                                                                                                     |
| ----------- | --------------------------------------------------------------------------------------------------------- |
| 15:10       | The private network CIDR changes from `10.50.0.0/16` to `10.51.0.0/16`. The atlas-vm guest loses access to the host. |
| 17:28       | The region moves to a new host. The data and the `metal` pool are copied. Image cleanup starts to fail.    |
| about 18:15 | `install_metal` starts metald. The host loses its gateway. The first theory is a proxy ARP conflict.      |
| 19:05       | metald starts again. WG Mesh reports `no buffer space available`. The host loses its gateway in 8 seconds. |
| 19:25       | The host reboots. The ZFS pool does not import, because `systemd-udev-settle` times out.                  |
| 19:38       | With unicast NDP and a capture that keeps the bond promiscuous, metald runs with 14 VMs.                  |
| 19:44       | `bnxt_re` is blacklisted. This removes the udev timeout.                                                  |
| 20:50       | A test without metald confirms the cause: `ip link set bond0.2 allmulticast on` stops all receive traffic. Turning allmulticast off recovers it. Promiscuous mode prevents it. |
| 21:20       | The WG Mesh fix is deployed. The bond is set to promiscuous mode through systemd-networkd.                |
| 22:45       | The tenant VMs start. The proxy cannot reach VMs of other tenants.                                        |
| 22:55       | The Metal Server is marked as provisioned. The next sync sends the privileged VM set. Sites respond.     |
| 23:01       | A reboot test passes without manual steps.                                                                |
| 23:05       | Warpgate trusts the new host key. The host firewall is enabled.                                           |

## Root cause

`prepareHost` in WG Mesh ran `ip link set <uplink> allmulticast on` on every host. The host uses a Broadcom `bnxt_en` NIC (firmware 214.4.9.9, kernel 6.8.0-146) in an 802.3ad bond, with the private network on a VLAN. On this NIC, allmulticast without promiscuous mode stops all receive traffic on both bond members within about 5 seconds. Without receive traffic, LACP and ARP fail, and the host goes offline.

Unicast NDP mode carries NDP in IPv4 unicast packets, so it does not need allmulticast. WG Mesh set allmulticast in both modes.

The driver can also turn on all-multicast by itself. `bnxt_en` filters at most 16 multicast addresses in hardware. Each proxy NDP entry joins one solicited-node group, so a host with many VMs goes above this limit in either NDP mode.

The fault is in NIC firmware `214.4.9.9/pkg 214.0.286.17`. Firmware `230.0.1.0/pkg 230.1.116.0` receives in all-multicast mode. On 2026-10-12, the same test on the same hardware stalled within 10 seconds on firmware 214 and passed on firmware 230.

### Contributing factors

- Each packet capture put the bond into promiscuous mode, which hid the fault. This made a multicast storm and proxy ARP look like the cause.
- The `bnxt_re` RoCE driver stalled `systemd-udev-settle` for 2 minutes at boot, so `zfs-import-cache` failed and metald had no pool.
- `install_metal` ran outside the provisioning flow, so `is_provisioning_completed` stayed `0`. Atlas skips the periodic sync for such a server, so WG Mesh had no privileged VMs, and it dropped proxy traffic to other tenants.
- The Metal Server kept its name, so its wg0 address did not change. Warpgate trusts only a new address, so it kept the host key of the old host.

## Resolution

- The NIC firmware on all hosts is upgraded to `230.0.1.0/pkg 230.1.116.0`. This is the fix. Use this version or later.
- The metald install sets allmulticast on the mesh uplink, and fails on `bnxt_en` firmware before 230 (PR #359).
- WG Mesh does not change the uplink receive mode (PR #354).
- The metald install blacklists `bnxt_re` on hosts with this NIC (PR #355).
- The Metal Server is marked as provisioned, and Warpgate has the new host key.

Before the upgrade, the hosts kept the bond in promiscuous mode as a workaround (PR #355). The upgrade removed the workaround.

## Lessons

- A packet capture changes the NIC state. Repeat a network test without the capture before you accept a cause.
- Set a NIC mode only when the selected feature needs it.
- When a NIC fails in a standard mode, compare its firmware with the latest vendor release first.
- A manual install step must leave the same Atlas state as the provisioning flow.
- A host that keeps its name but changes hardware needs a new trusted SSH host key.
