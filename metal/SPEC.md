# Metal Component Specification

[Root specification](../SPEC.md) · Behavior: [Metal codebase guide](../docs/develop/metal.md)

## Purpose

Metal manages virtual machines on a host with `metald`. It does not own the proxy or WG Mesh.

For Go code, follow the repository [Go anti-pattern rules](../llm/go-code-review-guide.md).

## Layout

```text
cmd/metald/     metald executable
internal/       packages, each with a SPEC.md
scripts/        host bootstrap scripts
test/           integration test data
Makefile        builds metald into dist/
```

The module path is `github.com/frappe/atlas/metal`. Run Go commands from `metal/`.

## Packages

| Package | Owns |
|---|---|
| [vm](internal/vm/SPEC.md) | VM manager, records, and host service interfaces. |
| [vm/migration](internal/vm/migration/SPEC.md) | Live migration and its transport. |
| [firecracker](internal/firecracker/SPEC.md) | Firecracker runtime and warm starts. |
| [api](internal/api/SPEC.md) | Thin HTTP handlers. |
| [host](internal/host/SPEC.md) | Host synchronization and capacity. |
| [console](internal/console/SPEC.md) | VM serial consoles over PTYs. |
| [reconciler](internal/reconciler/SPEC.md) | VM and image loops. |
| [storage](internal/storage/SPEC.md) | ZFS pool, disks, images, and snapshots. |
| [network](internal/network/SPEC.md) | VM networks and WireGuard peers. |
| [platform](internal/platform/SPEC.md) | Host files, commands, systemd. |

## Package imports

| Package | Imports |
| --- | --- |
| `cmd/metald` | api, host, reconciler, firecracker, storage, network, vm/migration, network/traffic |
| `api` | vm, host, console, vm/migration |
| `host` | vm, network, storage, vm/migration |
| `reconciler` | vm, storage, vm/migration |
| `firecracker` | vm, storage, platform, console, Firecracker API |
| `storage` | vm, platform |
| `network` | vm, platform, network/traffic |
| `vm/migration` | vm, storage, platform |
| `network/traffic` | cilium eBPF |

`vm` must not import `vm/migration`. It reads migration locks through an injected guard.

## Validation

See [Metal development](../docs/develop/metal.md). The [host layout](../docs/storage/host-layout.md) lists each host file and dataset.
