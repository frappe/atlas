# Atlas settings, providers, and host binaries

[Atlas app specification](../SPEC.md)

For the provider overview, see [docs/providers.md](../docs/providers.md).

## Purpose

Atlas talks to an infrastructure provider only through one interface. Everything provider-specific lives behind it, so adding a provider never reaches into server or virtual machine code.

This module also holds site settings, the regional token key, the wildcard TLS certificate, and the host binaries.

## Types

| Type | Owns |
|---|---|
| `AtlasSettings` (DocType) | Provider selection, credentials, the regional token key, and the published binary links. |
| `ServerProvider` | The contract every server provider implements. |
| `registry` | The map from a stable provider name to its implementation. |
| `DNSProvider`, `Route53Provider` | The DNS contract and its Route 53 implementation. |
| `host_binaries` | Building and publishing `metald` and the Atlas WG Mesh CLI. |
| `artifacts` | Publishing a build output as a public File, and its download URL. |
| `LetsEncrypt` | Wildcard certificate issuance through the dns-01 challenge. |
| `AcmeClient` | The ACME v2 conversation for one account key. |
| `certificate` | Reading a PEM chain, and checking a certificate against its key. |
| `SSHTask` (DocType) | Records one SSH command for a Metal Server or a Virtual Machine. |
| `ssh`, `parsing`, `mesh_address`, `object_storage` | Host access, strict input parsing, mesh addressing, and object storage. |

## Provider boundary

```text
server / vm code
      |
      v
ServerProvider  (typed create, result, catalog, power, address, and error values)
      |
      +-- scaleway/   client, servers, ip_addresses, catalog, partitioning, infrastructure
```

A provider component never saves a Frappe document. It returns typed values, and the caller decides what to record. That keeps provider code testable without a database and keeps persistence in one place.

Metal Server Size stores disk capacity in GiB and price in integer USD cents. The provider fills a missing billing period from the price it does report.

## Wildcard TLS

A wildcard name can only be proved through DNS, so `LetsEncrypt` answers the ACME dns-01 challenge with the configured `DnsProvider` and uses no other challenge type. `AcmeClient` speaks the protocol and touches no file. The account key is the only durable local state.

Atlas Settings owns the certificate chain, the private key, and the expiry. The expiry is read from the certificate on every validate, so the stored values cannot drift apart.

## SSH tasks

An `SSH Task` records one shell command or script run for a `Metal Server` or a `Virtual Machine`. Its `target` field is a Dynamic Link. The task reads the target document's `ssh_host` property for the connection address.

An `SSH Task` stores no credentials. It uses the identity of the Atlas host. The caller must make the target reachable.

Do not put a key or password in `script` or `environment`. These fields are stored as plain text. Use `SSHRunner` directly when the command needs secret data. See the [SSH Task README](doctype/ssh_task/README.md) for the creation methods and task states.

## Host binaries

A build runs only when its source hash changes. Atlas publishes the result as a public File because a host has no Atlas credential.

`artifacts` publishes build output and its download URL. It keeps a replaced File until no Atlas Settings link refers to it. The [Service module](../service/SPEC.md) uses the same helper for its package.

## Related

- [docs/providers.md](../docs/providers.md) describes the provider contract and how to add a provider.
- [docs/wildcard-tls.md](../docs/wildcard-tls.md) describes the certificate lifecycle and renewal.
- [docs/development.md](../docs/development.md) lists the build tools and manual build commands.
- [server SPEC](../metal_server/SPEC.md) describes the provider interface consumer.
