# Metal Server module specification

[Atlas app specification](../SPEC.md)

Behavior: [provisioning](../../docs/region/index.md), [hosts](../../docs/region/hosts-and-providers.md), [host sync](../../docs/region/host-sync.md), and [public IPs](../../docs/networking/public-ips.md). A Metal Server is a provider host that runs Metal.

## Types

| Type | Owns |
|---|---|
| `MetalServer` (DocType) | Lifecycle, permissions, whitelisted API |
| `provisioning` | Phase order, progress, failure logs |
| `host_installation` | Installs Metal on the host |
| `disk_inventory` | Block devices to Metal Server Disk rows |
| `catalog_sync` | Size and Image catalogs from the provider |
| `host_inspection` | Generic host registration. See [providers](../../docs/region/provider-guide.md#generic-provider). |
| `PublicIPPool` (DocType) | One address range and its attachment |
| `PublicIPAllocation` (DocType) | One tenant prefix and its requested VM attachment |
| `PublicIPService` | Allocation, attachment, and reconciliation |
| `MetalServerUsage` (DocType) | One capacity sample from Metal |

Host commands use [SSH Task](../atlas/doctype/ssh_task/) from the Atlas module.

## Invariants

- Provisioning commits after each phase. Every phase must be safe to repeat.
- Creation reuses the stored identity key, so a lost provider response cannot create a second host.
- The provider returns the `metald` listen address and the storage pool device. Host installation never searches for a disk.
- Certificate renewal restarts `metal.service` and shares the metald job lock with install and upgrade.
- Placement reads Metal Server Usage rows that `usage` writes after each `POST /v1/sync`.
- Atlas WG Mesh identifies a peer by `private_network_mac_address`. Sync writes it only when it changes.

## Related

- [Provider guide](../../docs/region/provider-guide.md): the provider contract.
- [Atlas settings module](../atlas/SPEC.md): provider implementations and settings.
