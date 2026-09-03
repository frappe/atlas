# Metal architecture

[metal SPEC](../SPEC.md) · packages: [internal SPEC](../internal/SPEC.md)

metald runs microVMs on one host. The premise is a stateless client: it turns HTTP
calls into per-VM systemd units and reaches each guest over its own socket. It keeps
no in-memory registry. All per-VM truth lives on disk.

## Components

```text
   HTTP client
       |
       v
   api  (Echo server over vm.VMDriver)
       |
       v
   firecracker.Driver ──> systemd (D-Bus)  ──> metal-vm@<id> ──> jailer ──> firecracker
       |                                                                       ^
       ├──> storage (ZFS)   kernel + rootfs + snapshots                        |
       ├──> network (netns) tap0 + veth + NAT                                  |
       └──> firecracker/api  REST over the VM socket  ─────────────────────────┘
```

metald is a client, not a parent. systemd owns each firecracker process, so a metald
restart or crash never kills a VM. Detail:
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

## Layering

`vm` defines the contract. `firecracker` implements it on the host packages. `api`
serves it. `cmd/metald` is the only place the concretes are built and injected. The
full dependency graph is in [internal/SPEC.md](../internal/SPEC.md).

## Stateless design

```text
per-VM truth on disk:  machines/<id>/config.json, snapshots/, the warmload mark
                       + the systemd unit state
on restart:            Load / List rebuild handles from disk, no registry to recover
```

Detail: [internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md) and
[docs/host-layout.md](host-layout.md).

## systemd unit model

Every VM is the template instance `metal-vm@<id>.service`. metald drives it over
D-Bus. Detail: [internal/systemd/SPEC.md](../internal/systemd/SPEC.md).

## Request flow

The API is synchronous. A handler calls the driver, blocks, and returns the settled
VM state. There is no polling. Detail: [internal/api/SPEC.md](../internal/api/SPEC.md).

## Read next

- [docs/vm.md](vm.md) the VM lifecycle and operations.
- [docs/storage.md](storage.md), [docs/networking.md](networking.md), [docs/snapshots.md](snapshots.md).
- [docs/host-layout.md](host-layout.md), [docs/api.md](api.md), [docs/testing.md](testing.md).
