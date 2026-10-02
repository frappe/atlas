# Code map

Use this page to go from a topic to its code. Each row names the handbook page, the specification (`SPEC.md`) that owns the details, and the place to start in the code. Read the specification before a structural change.

## Atlas app (Python, Frappe)

Tests sit beside the code as `test_*.py`.

| Area | Handbook | Specification | Start in code |
| --- | --- | --- | --- |
| Tenant API and auth | [Signing keys](../interfaces/signing-keys.md), [Tenant API](../interfaces/tenant-api.md) | [Atlas app](../../atlas/SPEC.md) | [Router](../../atlas/api/router.py), [authentication](../../atlas/auth/) |
| Placement | [Placement](../compute/placement.md) | [VM module](../../atlas/vm/SPEC.md) | [Placement package](../../atlas/vm/core/placement/) |
| VM create, change, delete | [VM records](../compute/vm-records.md) | [VM module](../../atlas/vm/SPEC.md) | [VM service](../../atlas/vm/core/vm_service.py) |
| Guest SSH keys | [Change SSH keys](../compute/ssh-keys.md) | [VM module](../../atlas/vm/SPEC.md) | [Metal key API](../../metal/internal/api/vm_ssh_keys.go), [guest key command](../../atlas/vm/scripts/guest/authorized-keys-command) |
| Calls to Metal | [How a VM request works](../start/how-a-vm-request-works.md) | [VM module](../../atlas/vm/SPEC.md) | [Metal client](../../atlas/vm/core/metal_client.py) |
| Migration and resize | [VM migration](../compute/migration.md) | [VM module](../../atlas/vm/SPEC.md) | [Migration](../../atlas/vm/core/vm_migration.py), [resize](../../atlas/vm/core/vm_resize.py) |
| Images | [Images](../storage/image-records.md) | [VM module](../../atlas/vm/SPEC.md) | [Image transfer](../../atlas/vm/core/vm_image_transfer.py) |
| Hosts and providers | [Hosts and providers](../region/hosts-and-providers.md) | [Metal Server module](../../atlas/metal_server/SPEC.md) | [Provisioning](../../atlas/metal_server/core/provisioning.py), [providers](../../atlas/atlas/core/server_providers/) |
| People access to hosts | [Host access](../region/host-access.md#people-access-through-warpgate) | [Service module](../../atlas/service/SPEC.md) | [Warpgate](../../atlas/service/core/warpgate/), [host API](../../atlas/api/routes/hosts.py) |
| Host sync | [Host sync](../region/host-sync.md) | [Metal Server module](../../atlas/metal_server/SPEC.md) | [Usage job](../../atlas/metal_server/usage.py) |
| Public IPs | [Public IPs](../networking/public-ips.md) | [Metal Server module](../../atlas/metal_server/SPEC.md) | [Public IP service](../../atlas/metal_server/core/public_ip_service.py) |
| Service VM lifecycle | [Service VMs](../region/service-vms.md) | [Service module](../../atlas/service/SPEC.md) | [Service records](../../atlas/service/doctype/) |
| Proxy setup | [Provision a proxy](../networking/http-proxy/provisioning.md) | [Service module](../../atlas/service/SPEC.md) | [Proxy provisioner](../../atlas/service/core/proxy/provisioning.py) |
| Cargo setup and bucket | [Cargo service](../region/cargo.md) | [Service module](../../atlas/service/SPEC.md) | [Cargo provisioner](../../atlas/service/core/cargo/provisioning.py), [bucket job](../../atlas/service/core/cargo/bucket.py) |
| IPv6 router setup | [IPv6 router](../networking/ipv6-router.md) | [Service module](../../atlas/service/SPEC.md) | [Router provisioner](../../atlas/service/core/ipv6_router/provisioning.py) |
| Settings and TLS | [Configuration](../region/configuration.md) | [Settings and providers](../../atlas/atlas/SPEC.md) | [Atlas Settings](../../atlas/atlas/doctype/atlas_settings/) |
| Browser console | [Console access](../compute/console.md) | [VM module](../../atlas/vm/SPEC.md) | [Realtime handlers](../../atlas/realtime/handlers.py) |
| Scheduled jobs | [Atlas troubleshooting](../operate/atlas.md) | [Atlas app](../../atlas/SPEC.md) | [Hooks](../../atlas/hooks.py) |

## Metal (Go, `metald`)

| Area | Handbook | Specification | Start in code |
| --- | --- | --- | --- |
| Daemon startup and config | [Daemon and API](../region/metald.md) | [metald](../../metal/cmd/metald/SPEC.md) | [Daemon entry](../../metal/cmd/metald/main.go) |
| HTTP API | [Daemon and API](../region/metald.md) | [API](../../metal/internal/api/SPEC.md) | [Routes](../../metal/internal/api/routes.go) |
| VM records and reconcile | [Reconciliation](../compute/reconciliation.md) | [VM](../../metal/internal/vm/SPEC.md) | [Manager](../../metal/internal/vm/manager.go), [reconciler](../../metal/internal/vm/reconcile.go) |
| Firecracker and systemd | [VM runtime](../compute/runtime.md) | [Firecracker](../../metal/internal/firecracker/SPEC.md) | [Machine](../../metal/internal/firecracker/machine.go) |
| Disks, images, snapshots | [Storage](../storage/host-storage.md) | [Storage](../../metal/internal/storage/SPEC.md) | [Storage package](../../metal/internal/storage/) |
| VM networking | [Networking](../networking/host-networking.md) | [Network](../../metal/internal/network/SPEC.md) | [Linux allocator](../../metal/internal/network/linux_allocator.go) |
| Migration | [Metal migration](../compute/migration-engine.md) | [Migration](../../metal/internal/vm/migration/SPEC.md) | [Migration package](../../metal/internal/vm/migration/) |
| Host sync | [Host sync](../region/host-sync.md) | [Host](../../metal/internal/host/SPEC.md) | [Host service](../../metal/internal/host/service.go) |
| Package map | [Metal overview](metal.md) | [Internal packages](../../metal/SPEC.md) | [Internal packages](../../metal/internal/) |

Tests sit next to the code as `*_test.go`. Host integration tests need a Linux host. See [integration testing](metal-testing.md).

## Network services

| Service | Handbook | Specification | Start in code |
| --- | --- | --- | --- |
| WG Mesh | [Network design](../networking/index.md), [development](wg-mesh.md) | [WG Mesh](../../services/wg-mesh/SPEC.md) | [BPF rules](../../services/wg-mesh/bpf/vm.h), [CLI](../../services/wg-mesh/cli/) |
| HTTP proxy | [HTTP proxy](../networking/http-proxy/index.md) | [HTTP proxy](../../services/http-proxy/SPEC.md) | [Control daemon](../../services/http-proxy/control/proxy_control/), [OpenResty](../../services/http-proxy/nginx/) |
| IPv6 router | [IPv6 router](../networking/ipv6-router.md) | [IPv6 router](../../services/ipv6-router/SPEC.md) | [BPF rules](../../services/ipv6-router/bpf/) |

The [repository map](../../SPEC.md) lists every component and its CI job.
