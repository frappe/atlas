# Metal Component Specification

[Root specification](../SPEC.md)

## Purpose

Metal manages virtual machines on a host. Its executable is `metald`.

## Layout

```text
cmd/metald/                  metald executable
internal/api/                HTTP API
internal/firecracker/        Firecracker support
internal/idalloc/            ID allocation
internal/network/            Linux networking
internal/storage/            ZFS storage
internal/systemd/            systemd and dbus support
internal/vm/                 VM domain logic
scripts/                     host bootstrap scripts
Makefile                     build metald into dist/
docs/                        API and test docs
test/                        Integration test data
```

## Software

Metal uses Go 1.26.2, Echo, Firecracker, systemd, dbus, ZFS, and Linux host features.

## Module

The module path is `github.com/frappe/atlas/metal`. Run Go commands from `metal/`.

## Validation

Run `go test ./...`. Run integration tests when host dependencies are available.
Run `make build` to build the stripped Linux binaries into `dist/`.

## Documentation

Read [`docs/host-layout.md`](docs/host-layout.md) for host paths and storage.
Read [`docs/api.md`](docs/api.md) for the API. Read [`docs/testing.md`](docs/testing.md) for tests.

## Scope

Metal manages host VMs. It does not define the proxy or WG Mesh services.

## Ownership

Keep VM logic in `internal/vm/`. Keep host integrations in their matching packages.
