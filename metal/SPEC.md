# Metal Component Specification

[Root specification](../SPEC.md)

## Purpose

Metal manages virtual machines on a host. Its executable is `metald`.

## Layout

```text
cmd/metald/                  metald executable            SPEC.md
internal/                    metald packages              SPEC.md (router)
internal/api/                HTTP API                     SPEC.md
internal/firecracker/        Firecracker support          SPEC.md
internal/firecracker/api/    firecracker REST client      SPEC.md
internal/idalloc/            ID allocation                SPEC.md
internal/network/            Linux networking             SPEC.md
internal/storage/            ZFS storage                  SPEC.md
internal/systemd/            systemd and dbus support     SPEC.md
internal/vm/                 VM domain logic              SPEC.md
scripts/                     host bootstrap scripts
Makefile                     build metald into dist/
docs/                        concept docs and references
test/                        Integration test data
```

Most packages have a `SPEC.md`. Start at [`internal/SPEC.md`](internal/SPEC.md) for
the package map and dependency graph, or [`cmd/metald/SPEC.md`](cmd/metald/SPEC.md)
for the daemon.

## Software

Metal uses Go 1.26.2, Echo, Firecracker, systemd, dbus, ZFS, and Linux host features.

## Module

The module path is `github.com/frappe/atlas/metal`. Run Go commands from `metal/`.

## Validation

Run `go test ./...`. Run integration tests when host dependencies are available.
Run `make build` to build the stripped Linux binaries into `dist/`.
Run `make openapi` to build the API specification. The binary embeds it, so run
this once after a clone or `go build` fails. `make build` runs it first, and
metald serves the result at `/docs`.

## Documentation

Follow [`docs/style.md`](docs/style.md) when you write or update docs.

Concept overviews:

- [`docs/architecture.md`](docs/architecture.md) the big picture and dependency graph.
- [`docs/vm.md`](docs/vm.md) the VM lifecycle and operations.
- [`docs/storage.md`](docs/storage.md) ZFS disks, snapshots, and images.
- [`docs/networking.md`](docs/networking.md) per-VM namespaces and NAT.
- [`docs/snapshots.md`](docs/snapshots.md) disk and memory snapshots, and warm images.

References:

- [`docs/host-layout.md`](docs/host-layout.md) host paths and storage.
- [`docs/api.md`](docs/api.md) the HTTP endpoint reference.
- [`docs/testing.md`](docs/testing.md) the integration test and config.

Package detail lives in each package's `SPEC.md`; see [`internal/SPEC.md`](internal/SPEC.md).

## Scope

Metal manages host VMs. It does not define the proxy or WG Mesh services.

## Ownership

Keep VM logic in `internal/vm/`. Keep host integrations in their matching packages.
