# Atlas WG Mesh

Atlas WG Mesh gives VMs static private IPv6 addresses and routes their traffic directly between bare-metal hosts. It uses eBPF for routing, WireGuard for encryption, and multicast discovery for VM locations.

## Scope

Atlas WG Mesh is a region-scoped private network. A VM keeps its address when it moves, and routing needs no controller or daemon. It requires a trusted IPv4 multicast Layer-2 network and does not manage WireGuard peers, keys, NAT, DNS, DHCP, or firewall rules.

## Limitations

Atlas WG Mesh does not provide inter-region VM connectivity yet.

## Documentation

- [Operations guide](docs/operations.md): requirements, build, installation, VM lifecycle, debugging, and upgrades.
- [Design guide](docs/design.md): addressing, [trust model](docs/design.md#trust-model), BPF maps and hooks, packet paths, and recovery behavior.
- [Benchmark results](docs/benchmark.md): throughput, packet rate, debug cost, and rate-limiter impact.
- [Debug in production](docs/debug-in-production.md): inspect routes and packet decisions.

## License

Atlas WG Mesh is licensed under [AGPL-3.0](../../license.txt).
