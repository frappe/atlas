# Service VMs

Atlas runs regional services in tenant-0 VMs on Metal hosts. They use the same VM lifecycle and WG Mesh as tenant VMs. Atlas grants mesh privilege because these services need to reach VMs across tenant boundaries.

Atlas owns each VM and its setup record. The software inside the VM owns its traffic or storage work.

| Service | Why it exists | Read next |
| --- | --- | --- |
| HTTP proxy | Accepts public web traffic and stores replicated routes. | [Proxy overview](../networking/http-proxy/index.md), [Atlas setup](../networking/http-proxy/provisioning.md) |
| Cargo | Runs image and object-storage services. | [Cargo setup and storage](cargo.md) |
| IPv6 router | Translates public IPv6 addresses to VM mesh addresses. | [Router setup and packet path](../networking/ipv6-router.md) |
| WireGuard gateway | Carries customer devices into the private addresses of their tenant. | [WireGuard gateway](../networking/wireguard-gateway.md) |

## What Atlas creates

Atlas creates privileged, termination-protected VMs through the normal VM service. The HTTP proxy and WireGuard gateway need a **tenant-0 public IPv4 allocation**. Reserve an address on the Public IP Pool form before you create either service.

The proxy guest firewall admits TCP 80 and 443 and ICMP from any address. It admits all traffic from tenant-0 mesh addresses in the region.

The gateway firewall admits its WireGuard UDP port, TCP 443, and ICMP from any address. It also admits traffic from the region mesh.

Atlas reaches service SSH through the VM host, not a public address. See [Atlas access to hosts](host-access.md#ssh).

The service record and VM have different states. For example, the VM can exist while its service record is still `Pending`. Do not infer service readiness from the VM state.

| Service status | Meaning |
| --- | --- |
| `Pending` | A VM is being created or a setup job is queued. |
| `Provisioning` | The setup job is working through service-specific steps. |
| `Active` | That service's setup checks passed. It does not prove future traffic or storage health. |
| `Failed` | A setup step failed. The Failure field names its phase. An operator must act. |
| `Archived` | Atlas removed the service record's VM and related resources. |

Cargo also starts as `Not Provisioned`. See [Cargo recovery](cargo.md#operate-and-recover) before replacing its VM.

## Publish a service address

A tenant-0 caller, such as Cargo, can set an A or AAAA record for a regional service. Atlas sends the change to the DNS provider at once and returns the provider error as `502 dns_provider_error`.

```http
PUT /api/atlas/dns-records/s3-svc.<domain>
{"address": "203.0.113.7", "ttl": 300}

PUT /api/atlas/dns-records/internal-s3-svc.<domain>
{"address": "fdaa:1:0:0:0:0:0:7"}
```

The label must be in `SERVICE_RECORD_LABELS` in `atlas/atlas/core/dns_providers/base.py`. A service name needs a public address. Its `internal-` name needs a WG Mesh address in `fdaa::/16`. `ttl` is optional, from 60 to 86400 seconds, with a default of 300. Setting an A record removes the AAAA record of the name in the same change, and the reverse. `DELETE` on the same path removes the A and AAAA records.

Metal looks up `internal-<host>` before it opens an image or snapshot connection to object storage, or a telemetry connection to Datum. When the answer is a private address, Metal connects to that address and keeps the original host name for TLS and presigned URLs. When the answer is public, or the private connection fails, Metal uses the public address.

Vector sends its Datum logs through a CONNECT proxy in metald at `127.0.0.1:9003`, so it uses the same private path and fallback. The proxy allows only the Datum host from `metald.toml` and at most 4 tunnels. `install-telemetry.sh` sets the proxy only when metald listens on that port, so a host with an older metald sends logs directly.

## Why a service can wait

Atlas queues a setup job after VM creation. If the VM is still a draft, the job leaves the service `Pending`. The scheduler queues it again.

::: info Check the service record first
A `Pending` record does not need a second provision request. A `Failed` record does not automatically retry. Read its Failure field and the linked SSH Task before taking the service-specific recovery action.
:::

## How packages reach a VM

The HTTP proxy, IPv6 router, and WireGuard gateway use the same package path. Atlas builds a reproducible archive from their service directories and publishes it as a File. Atlas Settings stores the file and its SHA-256 hash. The VM installer downloads the archive, checks the hash, and runs its setup script.

An unchanged archive is not published again. Proxy and gateway setup skip a package or configuration that already has the expected hash. Cargo uses its own installation script and does not use this package path.

::: details Source code and contracts

- [Service contract](../../atlas/service/SPEC.md) lists the service records and their VM rules.
- [Package builder](../../atlas/service/core/service_package.py) and [installer](../../atlas/scripts/install-service-package.sh) define the shared archive path.
- [Proxy Server](../../atlas/service/doctype/proxy_server/proxy_server.py), [Cargo Server](../../atlas/service/doctype/cargo_server/cargo_server.py), [IPv6 Router Server](../../atlas/service/doctype/ipv6_router_server/ipv6_router_server.py), and [Wireguard Gateway Server](../../atlas/service/doctype/wireguard_gateway_server/wireguard_gateway_server.py) own their service records.

:::
