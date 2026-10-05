# Metal Server module specification

[Atlas app specification](../SPEC.md)

Behavior: [provisioning](../../docs/region/index.md), [hosts](../../docs/region/hosts-and-providers.md), [Atlas access to hosts](../../docs/region/host-access.md), [host sync](../../docs/region/host-sync.md), and [public IPs](../../docs/networking/public-ips.md). A Metal Server is a provider host that runs Metal.

## Types

| Type | Owns |
|---|---|
| `MetalServer` (DocType) | Lifecycle, permissions, whitelisted API |
| `provisioning` | Phase order, progress, failure logs |
| `host_installation` | Installs Metal and the host firewall on the host |
| `AtlasPeer` | The Atlas wg0 identity and its `atlas0.conf` peer file |
| `DevelopmentGateway` | Development only. atlas-vm gateway mode on one host and the local `atlas-gateway.conf` |
| `MetalServer.import_from_provider` | Adds a provider server that Atlas did not create. Idempotent by provider server ID. |
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
- `metald` listens only on the host WireGuard address. The provider returns the storage pool device. Host installation never searches for a disk.
- `ssh_host` is the host WireGuard address once the host has a WireGuard key. Only setup before that uses the public address.
- `title` is one lowercase DNS label, unique across all hosts, including deleted ones (a database unique index). It is read-only; `before_insert` sets it to `metal-<region_name>-<counter>`. Warpgate names the host target and role after it.
- The Atlas peer lives in the host `wg0.conf`, not in host sync. Metal removes only the peers it added.
- Certificate renewal restarts `metal.service` and shares the metald job lock with install and upgrade.
- Placement reads Metal Server Usage rows that `usage` writes after each `POST /v1/sync`.
- Atlas WG Mesh identifies a peer by `private_network_mac_address`. Sync writes it only when it changes.
- `redfish_url`, `redfish_username`, and `redfish_password` are set together or not at all. The form and the registration dialog show them only when Atlas Settings enables BMC access for the Generic provider.

## Related

- [Provider guide](../../docs/region/provider-guide.md): the provider contract.
- [Atlas settings module](../atlas/SPEC.md): provider implementations and settings.
