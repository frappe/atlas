# Atlas settings, providers, and host binaries

[Atlas app specification](../SPEC.md)

Behavior: [provider guide](../../docs/region/provider-guide.md), [configuration](../../docs/region/configuration.md), [security](../../docs/interfaces/security.md), and [Service VMs](../../docs/region/service-vms.md). This module owns site settings, the provider boundary, TLS, and host binaries.

## Types

| Type | Owns |
|---|---|
| `AtlasSettings` (DocType) | Provider, credentials, token key, binary links |
| `AtlasSetup` | Deployer settings and first provider setup |
| `ServerProvider` | The provider contract |
| `registry` | Provider name to implementation |
| `DNSProvider`, `Route53Provider` | DNS contract and Route 53 |
| `host_binaries` | Builds `metald` and the WG Mesh CLI |
| `artifacts` | Publishes a build as a public File |
| `LetsEncrypt`, `AcmeClient` | Wildcard certificate through ACME |
| `certificate` | Authorities, certificates, and keys |
| `tls.metal` | Regional Metal authority and node certificates |
| `SSHTask` (DocType) | One SSH command for a host or VM |
| `ssh`, `parsing` | Host access and strict input parsing |
| `mesh_address`, `object_storage` | Mesh addressing and object storage |

## Settings validation

- The placement strategy must be a registered name. The default is `balanced`.
- `sleepy_vm_overcommit_factor` must be finite and at least `1.0`.
- `default_metal_machine_size` and `default_metal_machine_image` select the catalog entries for an auto-spawned host.

## Provider boundary

Host lifecycle code reaches a provider through `ServerProvider`. Implementations are Generic, Scaleway, AWS, and Redfish. Redfish registration reads an existing system through `RedfishClient`; remote host creation and provisioning remain unsupported. See the [Redfish provider guide](../../docs/region/provider-guide.md#redfish).

`redfish/client.py` returns typed `RedfishSystem` identities and `RedfishPowerStatus` observations. It validates HTTP resources and keeps links on one service. The provider resolves an active Metal Server by its canonical URL-derived identity and reads its per-server credentials. Neither the client nor provider saves documents or commits transactions. `redfish/test_client.py` covers discovery, observations, and error boundaries.

- A provider never saves a Frappe document. It returns typed values, and the caller records them.
- Metal Server Size stores disk in GiB and price in integer USD cents.

## TLS

- `AcmeClient` touches no file. The account key is its only local state.
- Atlas Settings reads the certificate expiry on every validate.
- The Metal authority key stays in Atlas Settings.
- Metal accepts only the client common name `atlas.<wildcard_domain>`.
- `ensure_server_certificate` reissues a node certificate that is missing, wrong, or within 30 days of expiry.
- Host installation sends node certificate files through direct SSH, never through an SSH Task.

## SSH tasks

- `target` is a Dynamic Link. The task connects to the target's `ssh_host` through its `get_ssh_proxy_command()`. A VM uses its host as the proxy.
- A task stores no credentials. It uses the Atlas host identity.
- `script` and `environment` are plain text. Use `SSHRunner` directly for secret data.

See the [SSH Task guide](doctype/ssh_task/) for creation methods and states.

## Host binaries

- A build runs only when its source hash changes.
- `artifacts` keeps a replaced File until no Atlas Settings link refers to it.
- The [Service module](../service/SPEC.md) uses `artifacts` for its package.

## Automatic setup

`configure-atlas` reads one JSON document from standard input. The `atlas-vm` setup script uses it, so secrets stay out of process arguments.

- Each external phase commits its state, because cloud and certificate operations cannot roll back.
- A repeated run updates mutable settings and rejects changes to completed provider identities.
- It uses an existing public Route 53 zone and never creates one.
- It syncs both catalogs and issues the wildcard certificate before it reports success.

## Related

- [Atlas development](../../docs/develop/atlas-app.md): build tools and commands.
- [Metal Server module](../metal_server/SPEC.md): the provider consumer.
