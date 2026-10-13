# WG Mesh Component Specification

[Root specification](../../SPEC.md)

## Purpose

WG Mesh gives VMs private IPv6 addresses. It routes traffic between bare-metal hosts. It also routes traffic the mesh does not own to a [gateway](../../docs/networking/wg-mesh/gateways.md) VM.

It uses eBPF, WireGuard, Linux NDP with proxy NDP, and NOT_HERE recovery. It serves one region.

## Layout

```text
bpf/                         eBPF hooks and maps
cli/                         Go CLI, one file per command group
cli/patches/                 Map layout patches, one file per version
tests/upgrade.sh             Upgrade check from one build to another
Makefile                     Build targets
```

## Software

The CLI uses Go. It builds for Linux with CGO disabled. The dataplane uses Clang and eBPF.

## Module

The module path is `github.com/frappe/atlas/services/wg-mesh/cli`. Run Go commands from `cli/`.

## Validation

From this directory, run `make bpf` and `make build`. Run `go vet ./...` from `cli/`. `TestChangedMapLayoutsHavePatches` requires a patch for each map that changes layout against `BPF_BASELINE_OBJECT`, which CI builds from the base branch. CI then runs `tests/upgrade.sh` from the base branch build. [Change a map](../../docs/networking/wg-mesh/operations.md#change-a-map) gives the rules. See the [design page](../../docs/networking/wg-mesh/index.md#hooks-and-state) for hooks.

## Documentation

Behavior lives in the handbook: [how VMs reach each other](../../docs/networking/index.md), then the [WG Mesh pages](../../docs/networking/wg-mesh/index.md).

## Scope

WG Mesh does not manage peers, keys, NAT, DNS, DHCP, firewalls, or inter-region links.

## Ownership

Keep eBPF code in `bpf/` and CLI code in `cli/`. Keep design and operation docs in `docs/networking/wg-mesh/`.
