# Unicast discovery networks

Atlas WG Mesh normally uses IPv4 multicast for discovery. Use this guide only when the participating hosts cannot share that multicast Layer-2 domain.

## When to use this

Some cloud providers do not provide multicast natively. Use the discovery relay there as a unicast alternative to the normal multicast network.

The relay forwards the multicast-style `WHO_HAS` and `NOW_HERE` messages to every configured peer. `FOUND` is already a unicast reply and does not go through the relay; it is sent with a time to live of 64, so it reaches a requester behind a router.

## Requirements

- Configure Atlas WG Mesh normally on every host.
- Permit each host to send UDP port `7373` to every other participating host.
- Create the same complete peer list on every host, using each host's reachable uplink IPv4 address. The local host is ignored if it appears, so the same file can be copied everywhere.
- Keep the peer file writable only by trusted administrators. Discovery messages are not authenticated.

For example, a peer file on host `10.20.0.10` can contain:

```text
10.20.0.11
10.20.0.12
```


## Start the relay

Run one relay on every participating host and keep it running with the host's service manager:

```sh
atlas-wg-mesh discovery-relay PEERS_FILE
```

Restart it automatically. A relay killed without running its cleanup leaves the BPF discovery redirect pointing at a TAP that no longer exists, and every `WHO_HAS` is dropped until the relay returns. `atlas-wg-mesh status` reports that state as a missing interface.

The relay creates an `atlas-wg-relay` TAP device and sets BPF's discovery redirect to that device. It sends `WHO_HAS` and `NOW_HERE` messages as UDP unicast to every peer in the file. On clean exit, it restores BPF discovery delivery to the physical uplink, returning the host to its normal multicast behavior.

A second relay exits rather than competing with the active one. `SIGTERM` and `SIGINT` use the clean shutdown path.

Use `--verbose` to log each accepted message and each peer send.

## Update peers

The relay checks the peer file modification time once per second. When the file changes, it parses and atomically adopts the new list. If a changed file is invalid, the relay logs a warning and continues to use its previous valid list.

Write a complete replacement file and rename it into place so the relay does not observe a partial edit.

## Upgrade

Run `atlas-wg-mesh upgrade` normally while the relay is running. The upgrade preserves the configured discovery interface, so the relay TAP stays active. Restart the relay after replacing its binary to run the new relay code.
