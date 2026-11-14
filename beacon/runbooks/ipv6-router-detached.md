# IPv6 router not forwarding

The tc eBPF program of an IPv6 router is not attached to `eth0`. The router VM runs, but it forwards nothing, so the public IPv6 addresses of the region that it serves stop.

1. Open a shell on the router VM. Run `systemctl status atlas-ipv6-router` and `tc filter show dev eth0 ingress`.
2. Run `systemctl restart atlas-ipv6-router`. It attaches the program again.
3. If the program does not attach, read `journalctl -u atlas-ipv6-router`. Run Install on the IPv6 Router Server in Atlas to build and attach it again.
