# Service VMs

Atlas runs regional services in tenant-0 VMs on Metal hosts. They use the same VM lifecycle and WG Mesh as tenant VMs. Atlas grants mesh privilege because these services need to reach VMs across tenant boundaries.

Atlas owns each VM and its setup record. The software inside the VM owns its traffic or storage work.

| Service | Why it exists | Read next |
| --- | --- | --- |
| HTTP proxy | Accepts public web traffic and stores replicated routes. | [Proxy overview](../networking/http-proxy/index.md), [Atlas setup](../networking/http-proxy/provisioning.md) |
| Cargo | Runs image and object-storage services. | [Cargo setup and storage](cargo.md) |
| IPv6 router | Translates public IPv6 addresses to VM mesh addresses. | [Router setup and packet path](../networking/ipv6-router.md) |
| WireGuard gateway | Carries customer tunnels into the private addresses of one tenant. | [Gateway setup and packet path](../../services/wg-gateway/README.md) |

## What Atlas creates

Reserve a **tenant-0 public IPv4 allocation** on the Public IP Pool form before you provision a service. Each service VM uses one. Atlas creates a privileged, termination-protected VM through the normal VM service and attaches that allocation.

The service record and VM have different states. For example, the VM can exist while its service record is still `Pending`. Do not infer service readiness from the VM state.

| Service status | Meaning |
| --- | --- |
| `Pending` | A VM is being created or a setup job is queued. |
| `Provisioning` | The setup job is working through service-specific steps. |
| `Active` | That service's setup checks passed. It does not prove future traffic or storage health. |
| `Failed` | A setup step failed. The Failure field names its phase. An operator must act. |
| `Archived` | Atlas removed the service record's VM and related resources. |

Cargo also starts as `Not Provisioned`. See [Cargo recovery](cargo.md#operate-and-recover) before replacing its VM.

## Why a service can wait

Atlas queues a setup job after VM creation. If the VM is still a draft, the job leaves the service `Pending`. The scheduler queues it again. The router waits for Metal to apply its public IPv4 address as well, because its next step replaces the full network configuration.

::: info Check the service record first
A `Pending` record does not need a second provision request. A `Failed` record does not automatically retry. Read its Failure field and the linked SSH Task before taking the service-specific recovery action.
:::

## How packages reach a VM

The HTTP proxy, IPv6 router, and WireGuard gateway use the same package path. Atlas builds a reproducible archive from their service directories and publishes it as a File. Atlas Settings stores the file and its SHA-256 hash. The VM installer downloads the archive, checks the hash, and runs its setup script.

An unchanged archive is not published again. Proxy setup also skips a package or configuration that already has the expected hash. Cargo uses its own installation script and does not use this package path.

::: details Source code and contracts

- [Service contract](../../atlas/service/SPEC.md) lists the service records and their VM rules.
- [Package builder](../../atlas/service/core/service_package.py) and [installer](../../atlas/scripts/install-service-package.sh) define the shared archive path.
- [Proxy Server](../../atlas/service/doctype/proxy_server/proxy_server.py), [Cargo Server](../../atlas/service/doctype/cargo_server/cargo_server.py), [IPv6 Router Server](../../atlas/service/doctype/ipv6_router_server/ipv6_router_server.py), and [WireGuard Gateway Server](../../atlas/service/doctype/wireguard_gateway_server/wireguard_gateway_server.py) own their service records.

:::
