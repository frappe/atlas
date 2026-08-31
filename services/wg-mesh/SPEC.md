# WG Mesh Component Specification

[Root specification](../../SPEC.md)

## Purpose

WG Mesh gives VMs private IPv6 addresses. It routes traffic between bare-metal hosts.

It uses eBPF, WireGuard, and multicast discovery. It serves one region.

## Layout

```text
bpf/                         eBPF source and headers
cli/                         Go CLI
docs/                        Design and operations docs
Makefile                     Build targets
```

## Software

The CLI uses Go. It builds for Linux with CGO disabled. The dataplane uses Clang and eBPF.

## Module

The module path is `github.com/frappe/atlas/services/wg-mesh/cli`. Run Go commands from `cli/`.

## Validation

From this directory, run `make bpf` and `make build`. Run Go tests from `cli/`.

## Documentation

Read [`README.md`](README.md) first. Then read the relevant file in [`docs/`](docs/).

## Scope

WG Mesh does not manage peers, keys, NAT, DNS, DHCP, firewalls, or inter-region links.

## Ownership

Keep eBPF code in `bpf/`. Keep CLI code in `cli/`. Keep design and operation docs in `docs/`.
