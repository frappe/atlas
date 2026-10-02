# Wireguard Gateway Server specification

[Service module specification](../../SPEC.md) · Behavior: [WireGuard gateway](../../../../docs/networking/wireguard-gateway.md) · [Gateway component](../../../../services/wg-gateway/SPEC.md)

## Lifecycle

- Records are `wireguard-NNN`. The number is the node number in `fdac` addresses, so a record name never changes.
- `before_insert` generates the node WireGuard key pair. The private key is a Password field and reaches only the node configuration.
- `WireGuardGatewayProvisioner` runs: network gateway role, node DNS, SSH, package, configuration, membership to the other nodes, readiness, regional DNS. The node joins `wireguard.<wildcard>` only after every node knows it. The regional health check uses `/readyz`.
- The member list holds nodes with `is_cluster_member`. Provisioning sets it after it configures the new node. `leave_cluster` clears it, removes DNS, stops the node API, and updates the surviving nodes in that order. If SSH cannot stop the API, Metal stops the VM first. Each external step follows a database commit, so Archive and Rebuild can run again after a failure.
- `GatewayConfiguration.digest` covers the member list, credentials, and certificate. `reconcile_gateway_configurations` sends a changed configuration each minute.
- Archive updates the surviving members before it terminates the old VM. It does not change the device table.
- `rebuild` replaces the VM with the shape that the record stores (`SHAPE_FIELDS`) and a new IPv4 allocation. `terminate_virtual_machine` skips a VM that is already terminating or gone. The node keeps its name, number, key, and devices. The node DNS TTL is 300 seconds, so devices find the new address soon.

## Return routes

`get_wireguard_gateway_routes` returns one `fdac` `/48` route per Active node. Host sync sends the set to every host. A VM opts in with `set_wireguard_gateway_access`. A network gateway cannot opt in.

## Access

Only System Managers with System User accounts can create, archive, or rebuild a node. Atlas Settings holds `wireguard_gateway_cluster_password`, which rotates with the proxy password.
