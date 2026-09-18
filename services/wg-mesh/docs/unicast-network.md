# Unicast discovery networks

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

Atlas WG Mesh normally uses NDP on a shared Layer-2 VLAN. Use the unicast mode when the participating hosts cannot share that Layer-2 domain, for example on providers that route all host traffic.

The unicast mode transports the same NDP packets over the routed IPv4 underlay. A TC egress hook wraps each solicitation or advertisement in an outer IPv4 header, and a TC ingress hook removes that header on the receiving peer. Linux neighbour discovery, proxy NDP, the Atlas NDP option, and the WireGuard datapath are unchanged.

## Requirements

- Configure Atlas WG Mesh normally on every host with `atlas-wg-mesh configure`. The Atlas neighbour kernel module is required in unicast mode, exactly as in multicast mode: load it with `atlas-wg-mesh module install`.
- Permit IPv4 protocol 41 between the participating hosts.
- Keep one peer file on every host with the reachable uplink IPv4 address of every other participating host. The same complete file can be copied everywhere: the local host drops its own address, so the file can list it too.
- Keep the peer file writable only by trusted administrators. Transported NDP is not authenticated, and a host with a listed address can influence learned VM locations.

A peer file can hold at most 256 addresses:

```text
10.20.0.11
10.20.0.12
```

## Manage the peer file

Use the `unicast peer` commands to edit the peer file. Every edit replaces the file atomically, so the daemon never reads a partial edit:

```sh
atlas-wg-mesh unicast peer add PEERS_FILE PEER
atlas-wg-mesh unicast peer remove PEERS_FILE PEER
atlas-wg-mesh unicast peer list PEERS_FILE
```

Add and remove are safe to repeat. An address that is already present or absent leaves the file unchanged.

The Atlas app knows every host address: the Metal Server document offers `get_mesh_peer_file`, which returns these contents from the running servers of the region.

## Start the daemon

Run one daemon on every participating host and keep it running with the host's service manager:

```sh
atlas-wg-mesh unicast start PEERS_FILE
```

The unicast hooks and the multicast NDP hook never run together. On start, the daemon attaches the two unicast TC hooks to the uplink and removes the multicast NDP filters. On a clean stop, it restores the multicast filters and removes the unicast hooks, and the host returns to multicast behavior. `SIGTERM` and `SIGINT` use the clean shutdown path. A second daemon exits rather than competing with the active one.

On start and on every adopted peer file change, the daemon adds one neighbour entry on the discovery interface for every peer, with `nud permanent extern_learn managed` and no MAC address. The kernel then resolves and maintains the MAC address on its own, which the BPF FIB lookup needs to send wrapped packets to a peer. A peer that leaves the file keeps its entry until the daemon restarts.

A start or stop passes through a short window where both hook sets are attached. That window is safe: the unicast hooks skip the work that the multicast hook already did, and every learning step is an idempotent replacement.

The daemon checks the peer file modification time once per second. When the file changes, it parses the file, installs the neighbour entries for the new peer set, and rewrites the pinned `peer_list` BPF map. The map holds the peers densely from index zero. If a changed file is invalid, or an install fails, the daemon logs a warning and keeps the previous list until the next successful pass.

The daemon performs no packet processing. All transport runs inside the BPF hooks.

## Atlas-managed unicast

When the Atlas app manages the host, the controller drives the unicast mode:

1. The `Use Unicast Networking` flag in Atlas Settings switches the region to unicast. The flag is off by default, and the region uses multicast NDP.
2. Every synchronization sends the complete `unicast_peers` set to each host with `POST /v1/sync`. The set lists the private IPv4 address of every running Metal Server, so every host receives the same set.
3. metald writes the set into the configured peer file and enables `atlas-wg-mesh-unicast.service`. The unit waits until `atlas-wg-mesh status` succeeds, so the daemon starts only after metald configured the host, and its start removes the multicast NDP filters that the configure run attached.
4. The daemon watches the peer file. A later synchronization that changes the set rewrites the file, and the daemon reloads the peer list map.
5. A synchronization without the `unicast_peers` field stops and disables the unit. The clean stop restores the multicast filters, so turning the flag off returns the region to multicast on the next synchronization.

A host that would keep no remote peer after it drops its own address does not run the daemon, so a single-host region stays in multicast mode.

The host installation writes the unit file and the peer file path into the metald configuration. A host that was installed before the unit existed must run the host installation again before the controller can switch it to unicast.

The `unicast peer` commands above remain for manual operation without the Atlas app.

## How discovery works

1. The host route for `fdaa::/16` still selects the uplink, so Linux sends a multicast solicitation on that interface when a VM location is unknown.
2. The unicast egress hook captures the solicitation before it reaches the wire. It appends a requester option that carries this host's IPv4 address, wraps the packet in an outer IPv4 header, and sends one copy to the peer that last answered for the target VM, or one copy to every peer when no owner is known. Every copy is a `bpf_clone_redirect` clone, so the copies share the packet data.
3. The peer ingress hook accepts the packet only when the outer IPv4 source is a configured peer. It removes the outer header and records the requester. Linux answers through proxy NDP.
4. The egress hook appends the TLLAO and Atlas options to the answer, wraps it, and returns it to the requester alone.
5. The requester ingress hook removes the outer header. It records the owning peer in `vm_peer_map`, the owner in `remote_vms`, and the neighbour entry through the Atlas kfunc, so Linux NUD owns liveness exactly as in multicast mode.

The owner entry is one shot: the egress hook removes it when it sends the solicitation. When the owner never answers, for example after a VM moved to another host, the next solicitation finds no entry and fans out to every peer. The new owner answers, and both maps point to it again.

Run `atlas-wg-mesh upgrade` normally while the daemon is running, then restart the daemon so it attaches the programs from the new release. The upgrade preserves the unicast maps.
