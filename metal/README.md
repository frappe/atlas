# Metal

Metal is the host daemon for Atlas virtual machines. The executable is `metald`.

Metal owns desired state, observed state, reconciliation, host resources, and cleanup progress. Atlas owns provider resources and user actions.

## Read first

1. Read the [architecture](docs/architecture.md).
2. Read the [virtual machine lifecycle](docs/vm.md).
3. Read the [current HTTP API](docs/api.md).
4. Read the [host layout](docs/host-layout.md).
5. Read the [development and test guide](docs/testing.md).

The [target `/v1` contract](../docs/metal-v1-contract.md) defines the atomic API change for Stage 4.

## Main packages

| Package | Purpose |
|---|---|
| `cmd/metald` | Compose and start the daemon. |
| `internal/api` | Expose controller operations. |
| `internal/vm` | Define virtual machine domain behavior. |
| `internal/firecracker` | Control Firecracker and its systemd unit. |
| `internal/network` | Own Linux network and WireGuard peer operations. |
| `internal/storage` | Own ZFS images, disks, and snapshot staging. |
| `internal/reconciler` | Apply current desired state. |

## Local checks

Run these commands from `metal/`:

```sh
go test ./...
go test -race ./...
go vet ./...
make openapi
make build
```

Host tests need Linux, root access, KVM, ZFS, systemd, iptables, and Atlas WG Mesh.
