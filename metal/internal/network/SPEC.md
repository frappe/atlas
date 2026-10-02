# network: VM and host network code contract

For Go code, follow the [Go review guide](../../../llm/go-code-review-guide.md). Read [host networking](../../../docs/networking/host-networking.md) for packet paths, addresses, firewall behavior, and limits. Read [sleepy VMs](../../../docs/compute/sleepy-vms.md) for traffic tracking behavior.

This package converges one VM network and applies the host WireGuard peer set. It does not allocate guest addresses. Each VM has a separate namespace.

## Owners

| Type | Code responsibility |
| --- | --- |
| `LinuxAllocator` | Implements `vm.Network`. Reads and converges one VM's namespace, links, routes, firewall, and traffic control. |
| `Mesh` | Registers VM addresses through the WG Mesh command. |
| `WireGuardManager` | Replaces the managed peers of one WireGuard interface. |
| `traffic.Monitor` | Owns TAP attachments, samples, packet events, and the event reader. |

## Convergence invariants

- `Ensure` stores no applied VM-network state. It reads host resources and accepts resources that already exist, so another pass can repair a partial failure.
- Remove unwanted links and routes before adding new ones. Add the veth first and mesh registration last so the first attracted packet has a complete path.
- Apply both firewall tables before exposing the VM through a veth, public address, or mesh registration. If the second table update fails, restore the first. The drift cache is memory only.
- A VM with a mesh address needs a veth pair. Only a warm-image capture VM has neither. `Release` removes mesh registration before deleting the namespace.
- `routes.go` restores the `default` prefix and host route lengths that `ip` omits. IPv4 NAT exists only while an IPv4 route uses `host`.
- Public-address rules are found by their `metal-public-ipv4-<vm-id>` or `metal-public-ipv6-<vm-id>` comments. Treat each rule set as one unit. For `iptables -C`, only exit code 1 means absent.
- Traffic policers sit on the namespace end of the veth. WG Mesh owns the host end. Private filters have priority over public filters.
- `LinuxAllocator` attaches monitoring after TAP creation and detaches it before TAP removal. `vm.Manager` decides when a VM sleeps. The monitor only reports activity.
- The TAP eBPF hook counts unicast packets and returns `TCX_NEXT`. IPv6 multicast must not keep an idle VM awake. Use attachment time as the idle baseline, not wall-clock time.
- `withNetworkNamespace` is the only in-process namespace helper. Only TCX attachment uses it.

## Host sync boundary

`EnsureHost` applies uplink and WireGuard interfaces. The WG Mesh command refuses an uplink change until an operator resets the mesh. `ApplyPrivilegedAddresses` and `WireGuardManager.Apply` each replace a complete set. `Apply` drops this host from its peer set, changes only previously managed peers, and rejects missing, invalid, or duplicate mesh addresses. `SyncPeerState` replaces the BPF peer map.

## Validation

Run `make bpf` before direct Go tests. The [integration tests](../../../docs/develop/metal-testing.md) need Linux, root, and the `integration` tag. The [VM contract](../vm/SPEC.md) defines `Network`. The [Firecracker contract](../firecracker/SPEC.md) attaches the guest to TAP.
