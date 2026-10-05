# Metal Server providers

Choose the provider configured for your region.

| Provider | Host model |
| --- | --- |
| [Generic](#generic-provider) | Register hosts you prepare. |
| [Scaleway](#scaleway) | Provider-managed Elastic Metal hosts. |
| [AWS](#aws) | EC2 hosts with one network interface and an Elastic IP. |

To add an adapter, start with [Add a provider](#add-a-provider).

See [Public IP management](../networking/public-ips.md) for direct and routed address behavior for each provider.

## Generic provider

Use the Generic provider for hosts that you prepare. Atlas does not create hosts or provider networks. It has no provider API, credentials, or catalog.

Atlas checks the prepared host, installs Atlas software, and manages VMs. The provider owns the host network and public IPv4 routes.

```text
Public IPv4 address
        |
        v
Provider VXLAN routes the address to every Metal Server
        |
        v
Metal adds the address to the VM port on its current host
```

This route makes a VM address available on any Metal Server. A VM can keep its public IPv4 address after migration.

You can subclass the Generic provider to add automation for one bare metal provider.

The **Enable BMC Access** option in Atlas Settings shows only for the Generic provider. It is off by default.

When it is on, you can store the BMC Redfish details of each host: the ComputerSystem URL, such as `https://10.0.0.5/redfish/v1/Systems/1`, the username, and the password. Enter all three or none.

The **Power State** field in the **BMC** section of the Metal Server form shows the live Redfish `PowerState`, such as `On` or `Off`. Atlas reads it each time you open the form. If the read fails, the form shows the reason at the top. Atlas does not change the host power through the BMC yet.

### Prepare a host

Before you register a host, prepare it with these items:

- Install Ubuntu or Debian on an x86_64 host with KVM.
- Give the host a public IPv4 address. Atlas connects as `root` with the Atlas public key.
- Give the host a private IPv4 address inside `private_network_cidr`.
- Put the private address on a provider network, such as a VXLAN interface. The interface must have an MTU of at least 1340.
- Configure the provider network to carry IPv6 multicast between hosts, or enable **Use Unicast Networking** in Atlas Settings. WG Mesh uses this network as its uplink.
- Configure the provider network to route every VM public IPv4 address to every Metal Server.
- Provide an empty whole disk for the storage pool. Alternatively, provide enough space on the root file system for a disk image.

Atlas does not create or change these network resources. It checks that the private IPv4 address exists before it installs WireGuard.

### Register a host

On the Metal Server list, select **Add Metal Server**. The dialog has 4 steps:

1. Enter the public and private IPv4 addresses. Atlas checks the addresses and starts a host check.
2. Wait for the host check. Atlas runs `generic/inspect-host.sh` as `root`. You cannot continue if a host check fails.
3. Select an empty disk for the storage pool. Atlas selects the disk when the host has one empty raw disk.
4. Review the host facts. Set the provider server ID and create the Metal Server. When BMC access is on, you can also enter the BMC details. You can change them later in the **BMC** section of the Metal Server form.

`HostInspection` in `metal_server/core/host_inspection.py` owns the host check. Atlas keeps the report in the cache for 30 minutes.

Use a raw disk when possible. If the host has no empty disk, step 3 lets you create a disk image. Atlas runs `generic/create-disk-image.sh` and uses `fallocate` to reserve `/root/disks/atlas.img`. The default size is 80% of the available space.

CAUTION: A disk image shares the root file system storage and I/O. Use a raw disk for better VM performance.

ZFS uses the file directly, without a loop device. Atlas rechecks the device before pool creation and trusts only cached inspection facts, not browser-supplied facts.

After reboot, `zfs-import-cache` reads `/etc/zfs/zpool.cache`. If the cache is lost, run:

```sh
zpool import -d /root/disks
```

The host check fails in these conditions:

- The host is not x86_64.
- The host has an unsupported operating system.
- The host has no KVM.
- No interface has the private IPv4 address.
- The private interface MTU is less than 1340.

Atlas accepts a disk only when it has no partitions, holders, partition table, signature, or mount. The disk must not be read-only or removable. A disk image must be a regular file with no signature. Atlas does not use loop devices as storage disks.

Atlas creates the Metal Server Size and Metal Server Image when they do not exist:

| Record | Name | Example |
|---|---|---|
| Metal Server Size | `<cpu_count>x<memory_gib>`, with memory rounded up | `32x128` |
| Metal Server Image | `<os> <os_version>` | `Ubuntu 24.04` |

An existing size must match the host architecture and CPU count. An existing image must match the host operating system.

Atlas stores the host facts in `provider_metadata`, including `storage_pool_device`. Provisioning does not create or prepare the host. It checks the private IPv4 address, then installs WireGuard and Metal.

### Add a static public IP pool

Open **Public IP Pool** and click **Add Public IP Pool**. The complete pool must be routed to every eligible Metal Server, for example through VXLAN.

| Address family | Allocation size |
| --- | --- |
| IPv4 | `/32`. For example, from `203.0.113.0/24`. |
| IPv6 | The prefix length each VM should receive. |

Attach and detach change only Metal's VM network. Atlas does not change provider routes for static pools.

### Unsupported operations

| Operation | Behavior |
|---|---|
| Automatic host creation | Refused. Atlas Settings rejects **Metal Auto-spawn Config > Enabled**. |
| Power actions | Refused. |
| Archive | Marks the record Deleted. The host keeps running, and the operator reclaims it. |
| Provider public IP reservation | Refused. Add a Static Public IP Pool. |
| Public IPv4 attach | Returns the public address. Metal adds the address to the VM port. |
| Public IPv4 detach and delete | Do nothing. |

## Scaleway

Atlas first creates or reuses a regional VPC, private network, and SSH key. It saves their provider IDs in Atlas Settings. The private network carries WireGuard packets and WG Mesh's host-location lookups.

For a Metal Server, Atlas finds an existing Scaleway server by its Atlas identity tag or requests one with the selected offer and operating system. It then:

1. Attaches the regional private network and waits for the provider install and attachment to finish.
2. Reads the private IPv4 address from Scaleway IPAM and the assigned VLAN number from the attachment.
3. Configures `eno1.<vlan>` with that address and the regional MTU. The public interface stays separate.
4. Continues with [WireGuard and Metal installation](index.md#provisioning-sequence).

On a two-disk offer, Atlas requests mirrored boot, root, and data arrays. The raw data array `/dev/md2` becomes Metal's ZFS pool device. This keeps VM data separate from the operating system. When Scaleway has no two-disk layout for that offer, Atlas leaves the provider's default partition schema unchanged.

Scaleway Flexible IPv4 addresses and IPv6 `/64` blocks attach to one server. A tenant VM can use direct delivery, but a provider-attached `/64` cannot follow several VMs to different hosts. Use the [IPv6 router](../networking/ipv6-router.md#why-atlas-uses-a-router) for independently moving VM `/128` addresses. [Public IP management](../networking/public-ips.md) explains the allocation modes.

::: details Provider source files

| Module | Owner |
|---|---|
| [configuration.py](../../atlas/atlas/core/server_providers/scaleway/configuration.py) | Immutable settings for low-level operations. |
| [client.py](../../atlas/atlas/core/server_providers/scaleway/client.py) | HTTP transport and Scaleway errors. |
| [infrastructure.py](../../atlas/atlas/core/server_providers/scaleway/infrastructure.py) | VPC, private network, and Secure Shell key operations. |
| [catalog.py](../../atlas/atlas/core/server_providers/scaleway/catalog.py) | Offer and operating system translation. |
| [servers.py](../../atlas/atlas/core/server_providers/scaleway/servers.py) | Elastic Metal server operations. |
| [ip_addresses.py](../../atlas/atlas/core/server_providers/scaleway/ip_addresses.py) | Flexible IP address operations. |
| [partitioning.py](../../atlas/atlas/core/server_providers/scaleway/partitioning.py) | Provider disk layout. |
| [provider.py](../../atlas/atlas/core/server_providers/scaleway/provider.py) | Contract composition and host network setup. |

:::

## AWS

::: details Provider source files

| Module | Owner |
|---|---|
| [configuration.py](../../atlas/atlas/core/server_providers/aws/configuration.py) | Immutable settings for low-level operations. |
| [client.py](../../atlas/atlas/core/server_providers/aws/client.py) | boto3 sessions, pagination, and AWS errors. |
| [infrastructure.py](../../atlas/atlas/core/server_providers/aws/infrastructure.py) | VPC, subnet, security group, and key pair operations. |
| [catalog.py](../../atlas/atlas/core/server_providers/aws/catalog.py) | Instance type and machine image translation. |
| [servers.py](../../atlas/atlas/core/server_providers/aws/servers.py) | Instance operations. |
| [ip_addresses.py](../../atlas/atlas/core/server_providers/aws/ip_addresses.py) | Host and VM Elastic IP address operations. |
| [provider.py](../../atlas/atlas/core/server_providers/aws/provider.py) | Contract composition and host network setup. |

:::

### Mesh discovery

WG Mesh finds a VM with NDP. A VPC does not carry link-local multicast, so an AWS region uses unicast networking: WG Mesh sends each solicitation to every peer over IPv4. Atlas Settings rejects AWS without **Use Unicast Networking**, and the automated setup enables it.

Atlas uses one subnet in one availability zone for the whole region. AWS delegates a public IPv6 `/80` to one host interface at a time.

The whole block can move to another host, but its VM `/128` addresses cannot spread across hosts by that attachment alone. Atlas uses the [IPv6 router](../networking/ipv6-router.md#why-atlas-uses-a-router) when VMs need to move independently.

### Host network shape

An Atlas host has one network interface in the Atlas subnet. The interface carries host traffic, mesh traffic, VM public addresses, and the delegated IPv6 blocks.

The Metal Server stores the interface name as both the public and the private network interface. Its primary private address is the private IPv4 address, and WG Mesh reaches each peer at that address.

The interface keeps the MTU of the VPC. **Private Network MTU** applies only to Scaleway.

### Host public address

Each host gets an Elastic IP on the primary private address of its interface. The address stays the same when the instance stops and starts.

Atlas finds the address by the `atlas-server` tag, so a retry reuses it. Atlas stores the allocation ID on the Metal Server, and deletion releases only that stored address.

### Network exposure

The Atlas security group allows all inbound traffic. The host firewall and Metal filter traffic on the host.

### Resource names

Each AWS resource has the name `Atlas - <region> - <detail>`, for example `Atlas - region-1 - security group`. Setup finds the internet gateway by its VPC and keeps a stored key pair name.

### Instance types

Atlas needs hardware virtualization. The catalog rejects instance types that have local instance storage.

A bare metal type provides the processor extensions. A virtual type must report `nested-virtualization` in `ProcessorInfo.SupportedFeatures`.

AWS keeps nested virtualization off until an instance asks for it, so Atlas launches a virtual instance with `CpuOptions.NestedVirtualization` set to `enabled`. A bare metal instance rejects that option, so Atlas does not send it.

`DescribeInstanceTypes` reports no price, so the catalog prices stay empty.

### Storage volumes

Atlas creates a 64 GiB `gp3` root volume and a 500 GiB `gp3` storage volume. Both volumes are part of the instance launch request.

The image metadata supplies the root device name. AWS does not give stable NVMe device names, so the provider sends the `/dev/disk/by-id` path of the storage volume. The path holds the volume ID without its dash.

Use **Resize Root Disk** or **Resize Storage Disk** to change EBS size, IOPS, or throughput.

- AWS refuses shrink requests and a second change within **6 hours**.
- After the guest sees the new size, `aws/grow-disk.sh` grows the root partition and ext4 filesystem, or expands ZFS with `zpool online -e`.

Both volumes have `DeleteOnTermination` enabled. A stop or a hardware change does not remove the storage volume, but instance deletion removes it.

### Public IPv4 addresses

AWS translates each public address to a secondary private address on the primary network interface. Atlas stores this private address as the host address.

Metal maps the host address to the VM. Detach uses the stored host address, so a retry can remove the secondary address after disassociation.

Atlas does not use the primary private address for a VM. This rule keeps the host public address separate from VM traffic.

## Ownership

Atlas owns provider selection, credentials, catalog records, and Metal Server documents. A provider owns remote resource operations.

Low-level provider components return values. They do not save Frappe documents or commit database transactions.

## Contract

| Area | Required operations |
| --- | --- |
| Configuration | Validate settings and credentials. Set up named infrastructure. |
| Catalog | Return sizes and images. |
| Host lifecycle | Create or reuse a host by Atlas name. Apply power actions. Delete safely. |
| Host preparation | Prepare resources before SSH. Configure networking after SSH. |
| Runtime inputs | Return the Metal bind address and storage pool device. |
| Optional public IPs | Reserve, attach, detach, and delete provider resources. |

Optional address operations raise `UnsupportedProviderOperation` when the provider does not support them.

`validate_server` checks the Metal Server values that a provider needs. The default accepts every Metal Server.

Creation uses `ServerCreateRequest` and returns `ProviderServer`. Catalog operations return `ServerSizeData` and `ServerImageData`.

## Add a provider

1. Add one package under `atlas/core/server_providers/`.
2. Write `ServerProvider` with absolute imports.
3. Register the class with `register`.
4. Split remote operations by owned provider resource.
5. Keep Frappe document writes in Atlas owners.
6. Add tests for registration, retries, power failures, deletion, and optional operations.
7. Add the provider option and fields to Atlas Settings.
8. Update this guide and the related specification.

Use `ServerCreateRequest.name` as the provider identity. The Metal Server UUID is unique across sites. Store the remote ID in `provider_server_id`.

Create the provider host only after Atlas commits the Pending record. Retries use the same name to find that host.

## Shared provider behavior

`ServerProvider` owns the operations that every provider repeats: `poll`, `run_setup_script`, `wait_for_private_address`, `apply_provider_server`, and `update_provider_metadata`. Set `error_class` on a provider class so these operations raise the error type of that provider.
