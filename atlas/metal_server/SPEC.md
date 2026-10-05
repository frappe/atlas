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
| `provider_registration` | Imports a registration-only host through `ServerProvider` and inserts or reuses its active record. |
| `ServerPower` | Applies provider power operations and reconciles lifecycle status. See [power state](../../docs/region/provider-guide.md#read-power-state). |
| `PublicIPPool` (DocType) | One address range and its attachment |
| `PublicIPAllocation` (DocType) | One tenant prefix and its requested VM attachment |
| `PublicIPService` | Allocation, attachment, and reconciliation |
| `MetalServerUsage` (DocType) | One capacity sample from Metal |

Host commands use [SSH Task](../atlas/doctype/ssh_task/) from the Atlas module.

## Invariants

- Provisioning commits after each phase. Every phase must be safe to repeat.
- Creation reuses the stored identity key, so a lost provider response cannot create a second host.
- Redfish registration identifies a ComputerSystem with GET requests. Its canonical URL determines the provider server ID. An advisory lock covers the active-record lookup and committed insert.
- A new Redfish record must use the registration API. It stays Pending, has optional size and image, and does not enqueue provisioning. Other providers require both catalog fields.
- `redfish_power_state` and `redfish_health` are virtual fields. `read_provider_power_status` shares one provider observation for the document in the current request. These fields are excluded from database writes. A failed read returns blank fields and an onload error.
- Virtual reads require the System Manager role and document read permission. They do not reconcile lifecycle status. New, Deleted, unregistered, and other-provider records do not contact Redfish.
- Explicit refresh and registration-only power actions share a per-server Redis lock. Provider calls finish before reconciliation takes a database row lock, reloads saved state, and rechecks the document guard.
- Reconciliation writes only lifecycle status. An Off observation marks the record Stopped. Power On changes Stopped to Pending. Reboot changes Running to Pending. No power observation promotes a host to Running.
- The Desk shutdown endpoint checks document write permission and submits the request without serializing virtual fields. The form reports acceptance without reloading or reading power again. Lifecycle status changes only after explicit reconciliation.
- Redfish shutdown and reboot require confirmation in Desk.
- `metald` listens only on the host WireGuard address. The provider returns the storage pool device. Host installation never searches for a disk.
- `ssh_host` is the host WireGuard address once the host has a WireGuard key. Only setup before that uses the public address.
- `title` is one lowercase DNS label, unique across all hosts, including deleted ones (a database unique index). It is read-only; `before_insert` sets it to `metal-<region_name>-<counter>`. Warpgate names the host target and role after it.
- The Atlas peer lives in the host `wg0.conf`, not in host sync. Metal removes only the peers it added.
- Certificate renewal restarts `metal.service` and shares the metald job lock with install and upgrade.
- Placement reads Metal Server Usage rows that `usage` writes after each `POST /v1/sync`.
- Atlas WG Mesh identifies a peer by `private_network_mac_address`. Sync writes it only when it changes.

## Related

- [Provider guide](../../docs/region/provider-guide.md): the provider contract.
- [Atlas settings module](../atlas/SPEC.md): provider implementations and settings.

## Validation

`core/test_provider_registration.py` covers registration. `core/test_server_power.py` covers power operations, lifecycle reconciliation, and changes during remote reads. `doctype/metal_server/test_redfish_virtual_fields.py` covers virtual serialization through the shared provider interface, request caching, failures, permissions, and the shutdown endpoint.

`doctype/metal_server/test_redfish_form.cjs` covers the Desk actions with Node's test runner. `doctype/metal_server/test_metal_server.py` covers the existing host lifecycle. Live checks are described in the [provider guide](../../docs/region/provider-guide.md#implementation-and-validation).
