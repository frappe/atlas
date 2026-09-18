# Atlas WG Mesh design

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

This document explains packet paths, BPF state, and recovery behavior. Read the [operations guide](operations.md) for setup.

## Contents

- [Atlas WG Mesh design](#atlas-wg-mesh-design)
  - [Contents](#contents)
  - [Addressing and state](#addressing-and-state)
  - [Discovery with NDP](#discovery-with-ndp)
  - [Trust model](#trust-model)
  - [BPF interface](#bpf-interface)
  - [System view](#system-view)
  - [Scenario 1: local VM delivery](#scenario-1-local-vm-delivery)
  - [Scenario 2: first packet to a remote VM](#scenario-2-first-packet-to-a-remote-vm)
  - [Scenario 3: known remote VM](#scenario-3-known-remote-vm)
  - [Scenario 4: receive an Atlas WG Mesh tunnel](#scenario-4-receive-an-atlas-wg-mesh-tunnel)
  - [Scenario 5: VM move](#scenario-5-vm-move)
  - [Scenario 6: unreachable owner](#scenario-6-unreachable-owner)
  - [Code map](#code-map)

## Addressing and state

Each host has a WireGuard address in `fdab::/16`. Each VM has an address in `fdaa::/16`.

```text
Bytes:  0  1 |  2  3 |  4  5  6  7 |  8  9 10 11 12 13 14 15
Field: fd aa | region |    tenant   |          VM ID
```

The data path uses the tenant field. Region and VM ID fields are available to provisioning.

Atlas WG Mesh owns traffic between VM addresses. A source address must be registered on the ingress VM interface, and VMs cannot reach host WireGuard addresses in `fdab::/16`. Linux owns other host traffic. WireGuard encrypts host-to-host traffic. Atlas WG Mesh does not configure WireGuard, NAT, DNS, DHCP, or firewall rules.

## Discovery with NDP

VM locations are learned with IPv6 neighbour discovery on the shared VLAN. There is no Atlas discovery daemon and no Atlas discovery protocol.

The host configuration installs one route for the VM range through the shared VLAN interface:

```text
fdaa::/16 dev <vlan>
```

Route lookup for a remote VM destination therefore selects the VLAN, and Linux runs NDP on that link. A more specific host route on the VM interface delivers traffic for local VMs.

When a VM is registered, the CLI adds a proxy NDP entry for the VM address on the VLAN interface. The host then answers neighbour solicitations for the VM with a neighbour advertisement. The NDP hook on the VLAN egress path appends the Atlas option to that advertisement:

```text
NDP option, type 253, 24 bytes:
    type      1 byte   253
    length    1 byte   3, in units of 8 bytes
    host      16 bytes the owning host's fdab::/16 address
    padding   6 bytes  zero
```

The option carries only the owning host WireGuard address. The NDP hook on the VLAN ingress path parses the option, records the VM location in `remote_vms`, and calls the Atlas kfunc. The kfunc, provided by the `atlas_neigh` kernel module, creates the Linux neighbour entry for the VM with `managed` and `extern_learn` semantics. Linux NUD then owns neighbour liveness: the kernel probes the entry, and every neighbour advertisement refreshes it and the recorded location.

The CLI is a lifecycle and configuration tool only. It never monitors neighbour state and it never configures WireGuard.

## Trust model

VMs use `fdaa::/16`; host WireGuard addresses use `fdab::/16`. The VM hook drops traffic to `fdab::/16`, so VMs cannot reach the host subnet through Atlas WG Mesh.

Nonzero tenant IDs are isolated from each other. Cross-tenant traffic is permitted only when either endpoint is a privileged tenant-`0` VM whose full IPv6 address is in the controller-managed whitelist. This allows request and response traffic while preventing access to every other tenant-`0` VM.

Neighbour advertisements are not authenticated, so a host with access to the shared VLAN can influence learned VM locations. Restrict the VLAN to trusted participating hosts. WireGuard encrypts traffic only to configured peers.

## BPF interface

| Hook | Program | Purpose |
| --- | --- | -- |
| VM interface | `handle_vm_packet` | Route local traffic, encapsulate remote traffic. |
| Shared VLAN interface | `handle_ndp_packet` | Add the Atlas option to outgoing advertisements, learn locations from incoming ones, register neighbours. |
| Shared VLAN interface | `handle_ndp_unicast_egress` | Unicast mode, attached instead of `handle_ndp_packet`: add the Atlas options, wrap solicitation and advertisement packets in IPv4, and send them to peers. |
| Shared VLAN interface | `handle_ndp_unicast_ingress` | Unicast mode, attached instead of `handle_ndp_packet`: validate the peer, learn requester, owner, and neighbour, remove the outer IPv4 header. |
| WireGuard interface | `handle_wireguard_packet` | Remove tunnel headers for local VMs, drop tunnels for others. |
| `neigh/neigh_update` tracepoint | `handle_atlas_nud` | Count consecutive NUD failures per remote VM, and drop the learned location and the neighbour entry after 20 failures. |

The VLAN hook is attached to both TC directions: egress adds the option to this host's own answers, ingress learns from other hosts' answers.

| Map | Purpose |
| --- | --- |
| `config` | Host configuration, including this host's WireGuard address. |
| `local_vms`, `remote_vms` | Local ownership and learned remote locations. |
| `nud_failures` | Consecutive NUD failures per remote VM. |
| `peer_list` | Unicast mode: peer IPv4 addresses, kept in sync from the peer file. |
| `vm_peer_map`, `ndp_requesters` | Unicast mode: last answering peer per VM, and peers waiting for an answer. |
| `privileged_tenant_allowed_addresses` | Privileged-tenant VM addresses permitted to communicate across tenants. |
| `debug_config`, `debug_stats`, `debug_events` | Optional debug state and events. |
| `build_hash` | Installed BPF object hash. |

## System view

```mermaid
flowchart LR
    subgraph Host_A["Host A"]
        VM_A[VM A]
        INTERFACE_A[VM interface]
        VM_HOOK["vm_hook.h<br/>TC ingress"]
        WG_A[wg0]
        VM_A --> INTERFACE_A --> VM_HOOK --> WG_A
    end

    VLAN["Shared VLAN<br/>IPv6 NDP"]

    subgraph Host_B["Host B"]
        WG_B[wg0]
        WG_HOOK["wireguard_hook.h<br/>TC ingress"]
        NDP_HOOK["ndp_hook.h<br/>TC ingress and egress"]
        INTERFACE_B[VM interface]
        VM_B[VM B]
        WG_B --> WG_HOOK --> INTERFACE_B --> VM_B
        VLAN --> NDP_HOOK
    end

    WG_A --> VLAN
```

The VM hook processes a packet from a VM interface. The WireGuard hook processes a packet after WireGuard decrypts it. The NDP hook processes neighbour solicitation and advertisement packets on the shared VLAN.

## Scenario 1: local VM delivery

This path applies when both VMs are connected to the same host.

```mermaid
sequenceDiagram
    participant A as VM A
    participant H as Host A vm_hook.h
    participant M as local_vms map
    participant B as VM B

    A->>H: IPv6 packet for VM B
    H->>M: Is VM B local?
    M-->>H: Yes
    H-->>B: Return packet to Linux routing
```

`vm_hook.h` returns `TC_ACT_OK`. Linux uses the host route for VM B and sends the packet to the destination interface. Atlas WG Mesh adds no tunnel header.

## Scenario 2: first packet to a remote VM

This path applies when the host has no location for the destination.

```mermaid
sequenceDiagram
    participant A as VM A
    participant HA as Host A vm_hook.h
    participant R as remote_vms map
    participant U as Shared VLAN
    participant HB as Host B ndp_hook.h

    A->>HA: IPv6 packet for VM B
    HA->>HA: No remote_vms record
    HA->>U: Packet continues to Linux routing
    Note over HA,U: The VLAN route selects the VLAN, so Linux sends an NS.
    U->>HB: NS: who has VM B?
    HB->>U: NA with Atlas option: fdab::B
    U->>HA: NA reaches the NDP hook
    HA->>R: Save Host B location, register neighbour
```

The first packet itself is delivered on the shared VLAN to the owning host. Linux routing on the owning host delivers it to the VM. Every later packet uses Scenario 3.

## Scenario 3: known remote VM

This path applies when `remote_vms` already has a location for the destination.

```mermaid
sequenceDiagram
    participant A as VM A
    participant HA as Host A vm_hook.h
    participant R as remote_vms map
    participant WGA as Host A wg0
    participant WGB as Host B wg0
    participant HB as Host B wireguard_hook.h
    participant B as VM B

    A->>HA: IPv6 packet for VM B
    HA->>R: Get location for VM B
    R-->>HA: Host B WireGuard address
    HA->>WGA: Add outer IPv6 header
    WGA->>WGB: WireGuard encrypted packet
    WGB->>HB: Decrypted tunnel packet
    HB-->>B: Remove outer header and route to VM interface
```

The outer IPv6 destination is the WireGuard address of Host B. The inner IPv6 packet stays unchanged.

## Scenario 4: receive an Atlas WG Mesh tunnel

This path applies after a remote host sends an Atlas WG Mesh IPv6-in-IPv6 tunnel.

```mermaid
flowchart LR
    A["WireGuard decrypts packet"] --> B["wireguard_hook.h reads outer IPv6 header"]
    B --> C{"Inner destination in local_vms?"}
    C -->|Yes| D["Remove outer IPv6 header"]
    D --> E["Linux routes inner packet to VM interface"]
    C -->|No| F["Drop the packet"]
```

The destination host never forwards an Atlas WG Mesh tunnel to another host. It either delivers the inner packet locally or drops it. A dropped tunnel means the sender holds a stale location; NDP refreshes the sender through its managed neighbour entry.

## Scenario 5: VM move

Use this path for a stopped VM. Remove the VM on the old host and add it on the new host before you start the guest.

```text
Host B: vm remove removes the /128 route and the proxy NDP entry.
Host C: vm add adds the /128 route and the proxy NDP entry.
```

The first neighbour solicitation from any sender reaches Host C, which answers with its own WireGuard address in the Atlas option. Senders update `remote_vms` and the kernel neighbour entry. No Atlas message and no WireGuard change are required.

## Scenario 6: unreachable owner

When an owner fails, senders keep their managed neighbour entries, so Linux NUD keeps probing. No host answers, the neighbour entries stay unresolved, and guest packets continue on the VLAN with no resolution. The operator purges the learned locations for the failed host:

```sh
atlas-wg-mesh remote purge --host fdab::2
```

## Unicast NDP transport

Hosts without a shared Layer-2 VLAN use the [unicast mode](unicast-network.md). The same NDP packets travel inside an outer IPv4 header between configured peers, so Linux neighbour discovery, proxy NDP, the Atlas option, and the WireGuard datapath stay unchanged.

The unicast hooks and `handle_ndp_packet` never run together. A host runs either the multicast NDP hook on the uplink, or the two unicast hooks, never both. The `unicast start` daemon owns the choice: it attaches the unicast hooks and removes the multicast filters, and a clean stop reverses the swap.

The egress unicast hook owns the whole advertisement path. For a solicitation, it appends a requester option that carries this host's IPv4 address, wraps the packet in IPv4, and sends one clone per peer with `bpf_clone_redirect`, which is copy on write. When `vm_peer_map` holds the last peer that answered for the target, only that peer receives a copy, and the entry is removed. For an answer, it appends the TLLAO and Atlas options itself, wraps the packet, and returns it to the requester alone.

The ingress unicast hook owns the whole learning path. It accepts a wrapped packet only from a configured peer, records the requester, removes the outer header, and restores the Ethernet type. For an answer with a valid Atlas option, it records the owning peer in `vm_peer_map`, the owner in `remote_vms`, and the neighbour entry through the Atlas kfunc, so Linux NUD owns liveness exactly as in multicast mode.

A one-shot owner entry makes the fan-out self healing. When the owner never answers, for example after a VM moved, the next solicitation finds no entry and fans out to every peer, and the new owner restores both `vm_peer_map` and `remote_vms`.

The `unicast start` daemon owns the peer file. It rewrites the `peer_list` map when the file changes. It processes no packets. A start or stop passes through a short window where both hook sets are attached; that window is safe, because the unicast hooks skip work that the multicast hook already did and every learning step is an idempotent replacement.

## NUD failure tracking

Linux NUD owns liveness, and a dead host stops answering probes. A managed neighbour then fails repeatedly, and the failure must remove the learned location, or senders keep routing to a host that no longer holds the VM.

`handle_atlas_nud` watches the `neigh/neigh_update` tracepoint. A `NUD_REACHABLE` event resets the failure count for the VM, and a `NUD_FAILED` event increments it. After 20 consecutive failures, the hook removes the `remote_vms` entry, clears the Linux neighbour entry for garbage collection through the Atlas delete kfunc, and removes the counter. The next solicitation then fans out and relearns the VM location.

The hook reads the trace event flags as one byte, so the managed bit at bit 8 is lost. The externally-learned bit at bit 4 survives, and only Atlas registrations set it, so the hook filters on that bit together with the `remote_vms` membership.

The hook is not a tc hook, so it owns no tc filter. `configure` attaches it to the tracepoint and pins the link in the pin directory, and a pinned link keeps the hook attached after the CLI exits. `upgrade` replaces the link, because a perf event link cannot update its program. `reset` removes the pin directory, which releases the link and detaches the hook.

## Code map

| File | Main responsibility |
| --- | --- |
| `bpf/bpf.c` | Build one BPF object from all source fragments. |
| `bpf/vm_hook.h` | Process packets from VM interfaces. |
| `bpf/ndp_hook.h` | Process NDP packets on the shared VLAN interface. |
| `bpf/ndp_unicast_hook.h` | Transport NDP packets over a routed IPv4 underlay. |
| `bpf/nud_hook.h` | Drop unresponsive remote VMs after repeated NUD failures. |
| `bpf/wireguard_hook.h` | Process Atlas WG Mesh tunnels. |
| `bpf/state.h` | Define host and VM BPF state. |
| `bpf/debug.h` | Define debug maps and event helpers. |
| `bpf/protocol.h` | Define on-wire values and structures, including the Atlas NDP option. |
| `bpf/address.h` | Define VM address helpers. |
| `kernel/atlas_neigh.c` | Provide the neighbour registration and deletion kfuncs. |
