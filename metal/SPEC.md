# Metal Component Specification

[Root specification](../SPEC.md)

## Purpose

Metal manages VMs on a host. Its executable is `metald`.

## Layout

```text
cmd/metald/                  metald executable
internal/api/                HTTP API
internal/firecracker/        Firecracker support
internal/idalloc/            ID allocation
internal/network/            Linux networking
internal/storage/            LVM storage
internal/systemd/            systemd and dbus support
internal/vm/                 VM domain logic
deploy/                      systemd units
docs/                        API and test docs
test/                        Integration test data
```

## Software

Metal uses Go 1.26.2, Echo, Firecracker, systemd, dbus, LVM, and Linux host features.

## Module

The module path is `github.com/frappe/atlas/metal`. Run Go commands from `metal/`.

## Validation

Run `go test ./...`. Run integration tests when host dependencies are available.

## Documentation

Read [`docs/api.md`](docs/api.md) for the API. Read [`docs/testing.md`](docs/testing.md) for tests.

## Scope

Metal manages host VMs. It does not define the proxy or WG Mesh services.

## Ownership

Keep VM logic in `internal/vm/`. Keep host integrations in their matching packages.
