# Host mesh peers not connected

The host has no WireGuard handshake on `wg0` with at least half of its peer hosts for 3 minutes or more. VMs on this host cannot reach VMs on those hosts over the private network. The detail names up to 3 peer hosts. A host that lost fewer than half of its peers does not alert, so the alert names the host at fault. When two hosts alert, the link between them can be at fault.

1. On the host, run `wg show wg0 latest-handshakes`. A peer with `0` or an old time has no tunnel.
2. Check the uplink: `ip -br link` and `ip route show default`. A down uplink also stops the tunnels.
3. Check that UDP 51820 is open on both hosts, also in the host firewall: `nft list table inet atlas_host`.
4. Check that `wg-quick@wg0.service` runs. If you restart it, restart `metal.service` after it, because only metald attaches the WG Mesh program to `wg0`. See the [incident report](../../docs/incidents/2026-10-16-wireguard-restart-detached-mesh.md).
5. Read [Diagnose a host](../../docs/operate/diagnose-a-host.md) and [How Atlas reaches hosts](../../docs/region/host-access.md#recovery). Escalate to the Atlas team when the tunnel does not come back.
