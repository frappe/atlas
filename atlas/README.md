# Atlas app

The Atlas Frappe app owns provider integration, Server records, placement, images, and user actions.

Atlas sends desired virtual machine state to Metal. Metal owns runtime state and host resources.

## Main areas

| Area | Purpose | Start here |
|---|---|---|
| Atlas settings | Provider selection, credentials, region data, and host binaries | [Atlas module](atlas/README.md) |
| Servers | Provider hosts and Metal installation | [Server module](server/README.md) |
| Virtual machines | Placement, Metal requests, images, and user workflows | [Virtual machine module](vm/README.md) |
| Realtime | Console WebSocket bridge | [Realtime specification](realtime/SPEC.md) |

## Concepts

- [Provider contract and extension guide](docs/providers.md)
- [Server lifecycle](docs/server-lifecycle.md)
- [Development and tests](docs/development.md)
- [System architecture](../docs/architecture.md)
- [Target Metal `/v1` contract](../docs/metal-v1-contract.md)

## Boundaries

Keep DocType methods as permission and API boundaries. Put provider behavior in `atlas/core/server_providers/`.

Put server setup behavior in `server/core/`. Put virtual machine orchestration in `vm/core/`.

Do not store mutable Metal runtime state in DocType fields.
