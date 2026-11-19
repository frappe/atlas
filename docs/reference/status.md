# Status

This page lists current limitations, experimental features, and planned work in one place. Each owning page explains the behavior in detail. Read it before you plan work, so that you do not "fix" an accepted trade-off.

## Limitations

| Limitation | Owner |
| --- | --- |
| A region binds to one provider. Atlas does not span providers or connect regions. | [What Atlas is](../start/what-atlas-is.md) |
| A `metald` restart keeps running VMs only when the systemd descriptor store holds their consoles. | [VM runtime](../compute/runtime.md) |
| Placement ignores a host whose capacity sample is older than two minutes. | [Placement](../compute/placement.md) |
| An uncertain create keeps its draft and capacity reservation until Atlas checks Metal. | [VM request](../start/how-a-vm-request-works.md) |
| The first packet can be lost during WG Mesh discovery or an idle VM restore. | [WG Mesh](../networking/wg-mesh/index.md), [sleepy VMs](../compute/sleepy-vms.md) |
| A source host runs one outbound migration stream at a time. | [Metal migration](../compute/migration-engine.md) |
| Proxy route writes need a leader and enough node acknowledgements. | [HTTP proxy](../networking/http-proxy/index.md) |
| Atlas installs Cargo with a placeholder Central URL. | [Cargo service](../region/cargo.md) |
| Atlas API tokens have no route scopes, and Atlas has no tenant quotas. | [Security model](../interfaces/security.md#limitations) |

## Experimental

| Feature | Owner |
| --- | --- |
| The file-backed ZFS setup script is for development only. | [Metal storage](../storage/host-storage.md#experimental) |
| The WG Mesh `NOT_HERE` repair packet uses experimental IPv6 next-header value `253`. | [WG Mesh](../networking/wg-mesh/index.md#experimental) |
| The placement simulators run outside the request path. | [Placement](../compute/placement.md#experimental) |

## Planned work

These are the next major changes. None of them is current behavior.

| Change | Goal | Current owner |
| --- | --- | --- |
| Faster migration | Reduce the time to move a VM between hosts. | [Migration flow](../compute/migration.md) |
| Topology-based placement | Place VMs by host topology, for example an anti-affinity rule that keeps replicas in different racks. | [Placement](../compute/placement.md) |
| Network volumes | Attach storage that is not local to one host. | [Metal storage](../storage/host-storage.md) |
| Liquid VM | Extend sleepy VMs so that a saved VM can move easily between hosts. | [VM runtime](../compute/runtime.md) |
| Abuse protection | Export per-VM traffic metrics and logs from the hosts, and apply the restrictions that Central decides in the data plane. | [Abuse protection](../abuse/index.md) |
