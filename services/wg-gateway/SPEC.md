# WireGuard gateway component specification

[Root specification](../../SPEC.md) · Behavior: [WireGuard gateway](../../docs/networking/wireguard-gateway.md)

## Interfaces

- `setup.sh` takes `REGION_ID`, `GATEWAY_ID`, `GATEWAY_MESH`, and `LISTEN_PORT`. It compiles `bpf/gateway.c` with them, creates `wg0`, and installs the daemon with the [control-cluster](../control-cluster/SPEC.md) package.
- Atlas writes `/etc/atlas/wireguard-gateway.toml`: `[gateway]` with the node key, `[[nodes]]` with every member, `[auth]`, `[cluster]`, and `[tls]`. The daemon refuses a partial file.
- The daemon serves HTTPS on `[::]:443`: `POST`, `PUT`, `DELETE`, and `GET` on `/v1/peers`, `/healthz`, `/readyz`, `/docs`, and `/docs/swagger.json`.

## Invariants

- `PeerState` holds every device as `"<tenant>:<client>" → {public_key, node_id}`. A mutation changes one device, or replaces the complete table.
- A node writes only its own devices to `wg0`. A device keeps its node, because its address carries the node number.
- A public key belongs to one device. A device keeps its first public key until it is removed.
- A new device needs a current node that serves: the leader places it only on a member whose heartbeat reports a configured `wg0`.
- The configuration must list this node in `[cluster]`, also for a single node. The table keeps devices of an archived node, and their address comes from the node number in `node_id`.
- The eBPF filters require the same region and tenant in the device and VM addresses. No address translation occurs.

## Validation

From this directory, run `python -m pip install --editable ../control-cluster --editable 'daemon[test]'`, `python -m ruff check daemon`, and `python -m pytest -q daemon/tests`.
