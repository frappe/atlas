# Atlas WG Mesh

Atlas WG Mesh gives VMs static private IPv6 addresses and routes their traffic directly between bare-metal hosts. It uses eBPF for routing, WireGuard for encryption, and Linux NDP with proxy NDP for VM location discovery.

For Go code, follow the repository [Go anti-pattern rules](../../llm/go-code-review-guide.md).

## Scope

Atlas WG Mesh is a region-scoped private network. A VM keeps its address when it moves, and routing needs no controller or daemon. It requires a trusted shared VLAN for NDP, or the unicast mode for routed networks, and does not manage WireGuard peers, keys, NAT, DNS, DHCP, or firewall rules.

## Limitations

Atlas WG Mesh does not provide inter-region VM connectivity yet.

## Documentation

- [Operations guide](docs/operations.md): requirements, build, installation, VM lifecycle, debugging, and upgrades.
- [Design guide](docs/design.md): addressing, [trust model](docs/design.md#trust-model), NDP discovery, BPF maps and hooks, and packet paths.
- [Unicast network guide](docs/unicast-network.md): NDP transport over a routed IPv4 underlay.
- [Benchmark results](docs/benchmark.md): throughput, packet rate, and debug cost.
- [Debug in production](docs/debug-in-production.md): inspect routes and packet decisions.

## License

Atlas WG Mesh is licensed under [AGPL-3.0](../../license.txt).
