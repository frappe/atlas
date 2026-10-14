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
| `DevelopmentListener` | Development only. `start-dev-listener` serves `atlas_internal_url` and forwards to the local site |
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
<<<<<<< HEAD
=======
- `usage` validates optional host compatibility facts and stores `host_compatibility_report` and `tsc_scaling_supported` only when they change. Migration decisions stay in Atlas. See [host sync](../../docs/region/host-sync.md).
>>>>>>> 3fe1e63 (refactor(atlas): Tidy provider and host sync internals)
- Atlas WG Mesh identifies a peer by `private_network_mac_address`. Sync writes it only when it changes.
- The Generic provider requires `bmc_url`, `bmc_username`, and `bmc_password` together or not at all. Desk shows these fields only when BMC access is enabled.
- `bmc_power_state` is virtual and reads the provider at most once per request. Generic hosts need Redfish and a BMC URL. Read failures set `__onload.bmc_power_error` without failing the form load.

## Related

- [Provider guide](../../docs/region/provider-guide.md): the provider contract.
- [Atlas settings module](../atlas/SPEC.md): provider implementations and settings.
