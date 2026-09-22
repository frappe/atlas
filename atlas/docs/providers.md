# Metal Server providers

Atlas uses `ServerProvider` as the server provider extension point. The registry maps one stable provider type to one provider class. Atlas has three providers: Generic, Scaleway, and AWS.

## Ownership

Atlas owns provider selection, credentials, catalog records, and Metal Server documents. A provider owns remote resource operations.

Low-level provider components return values. They do not save Frappe documents or commit database transactions.

## Contract

The provider contract includes these operations:

- Validate settings and credentials.
- Set up named provider infrastructure.
- Return server sizes and images.
- Ensure one provider host by its Atlas name.
- Prepare provider resources before Secure Shell access.
- Configure the provider network after Secure Shell access.
- Return the address that metald can bind.
- Apply one explicit power action.
- Delete one provider host safely.
- Return the storage pool device.
- Optionally reserve, attach, detach, and delete public IPv4 addresses.

Optional address operations raise `UnsupportedProviderOperation` when the provider does not support them.

`validate_server` checks the Metal Server values that a provider needs. The default accepts every Metal Server.

Creation uses `ServerCreateRequest` and returns `ProviderServer`. Catalog operations return `ServerSizeData` and `ServerImageData`.

## Add a provider

1. Add one package under `atlas/core/server_providers/`.
2. Implement `ServerProvider` with absolute imports.
3. Register the class with `register`.
4. Split remote operations by owned provider resource.
5. Keep Frappe document writes in Atlas owners.
6. Add tests for registration, retries, power failures, deletion, and optional operations.
7. Add the provider option and fields to Atlas Settings.
8. Update this guide and the related specification.

Use `ServerCreateRequest.name` for provider identity. A Metal Server is named with a UUID, which is unique across Atlas sites. Keep the provider host ID in `provider_server_id`. The provider host is created only after Atlas commits the Pending Metal Server record. A retry uses the same name to find the same host.

## Shared provider behavior

`ServerProvider` owns the operations that every provider repeats: `poll`, `run_setup_script`, `wait_for_private_address`, `apply_provider_server`, and `update_provider_metadata`. Set `error_class` on a provider class so these operations raise the error type of that provider.

## Generic provider

Use the Generic provider for hosts that you prepare. Atlas does not create hosts or provider networks. It has no provider API, credentials, or catalog.

Atlas checks the prepared host, installs Atlas software, and manages virtual machines. The provider owns the host network and public IPv4 routes.

```text
Public IPv4 address
        |
        v
Provider VXLAN routes the address to every Metal Server
        |
        v
Metal adds the address to the virtual machine port on its current host
```

This route makes a VM address available on any Metal Server. A VM can keep its public IPv4 address after migration.

You can subclass the Generic provider to add automation for one bare metal provider.

### Prepare a host

Before you register a host, prepare it with these items:

- Install Ubuntu or Debian on an x86_64 host with KVM.
- Give the host a public IPv4 address. Atlas connects as `root` with the Atlas public key.
- Give the host a private IPv4 address inside `private_network_cidr`.
- Put the private address on a provider network, such as a VXLAN interface. The interface must have an MTU of at least 1340.
- Configure the provider network to carry multicast. WG Mesh uses this network as its uplink.
- Configure the provider network to route every VM public IPv4 address to every Metal Server.
- Provide an empty whole disk for the storage pool. Alternatively, provide enough space on the root file system for a disk image.

Atlas does not create or change these network resources. It checks that the private IPv4 address exists before it installs WireGuard.

### Register a host

On the Metal Server list, select **Add Metal Server**. The dialog has 4 steps:

1. Enter the public and private IPv4 addresses. Atlas checks the addresses and starts a host check.
2. Wait for the host check. Atlas runs `generic/inspect-host.sh` as `root`. You cannot continue if a host check fails.
3. Select an empty disk for the storage pool. Atlas selects the disk when the host has one empty raw disk.
4. Review the host facts. Set the provider server ID and create the Metal Server.

`HostInspection` in `metal_server/core/host_inspection.py` owns the host check. Atlas keeps the report in the cache for 30 minutes.

Use a raw disk when possible. If the host has no empty disk, step 3 lets you create a disk image. Atlas runs `generic/create-disk-image.sh` and uses `fallocate` to reserve `/root/disks/atlas.img`. The default size is 80% of the available space.

CAUTION: A disk image shares the root file system storage and I/O. Use a raw disk for better VM performance.

ZFS uses the file directly. It does not use a loop device. Atlas checks the selected storage device again before it creates the ZFS pool. After a reboot, `zfs-import-cache` opens the file from `/etc/zfs/zpool.cache`. If this cache file is lost, run `zpool import -d /root/disks`. Atlas reads host facts from the cached report. It does not accept host facts from the browser.

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

### Add a public IPv4 address

On the Metal Server IP Address list, select **Add**. Enter an IPv4 address that the provider network routes to every Metal Server through VXLAN. Confirm this route when you save the address.

For Generic, the IPv4 address is also the provider resource ID. Atlas sends this address to Metal when it attaches the address to a VM. Atlas does not change the provider route during attach or detach.

### Unsupported operations

| Operation | Behavior |
|---|---|
| Automatic host creation | Refused. Atlas Settings rejects **Metal Auto-spawn Config > Enabled**. |
| Power actions | Refused. |
| Archive | Marks the record Deleted. The host keeps running, and the operator reclaims it. |
| Public IPv4 reservation | Refused. Use **Add** on the Metal Server IP Address list. |
| Public IPv4 attach | Returns the public address. Metal adds the address to the VM port. |
| Public IPv4 detach and delete | Do nothing. |

## Scaleway structure

| Module | Owner |
|---|---|
| `configuration.py` | Immutable settings for low-level operations. |
| `client.py` | HTTP transport and Scaleway errors. |
| `infrastructure.py` | VPC, private network, and Secure Shell key operations. |
| `catalog.py` | Offer and operating system translation. |
| `servers.py` | Elastic Metal server operations. |
| `ip_addresses.py` | Flexible IP address operations. |
| `partitioning.py` | Provider disk layout. |
| `provider.py` | Contract composition and host network setup. |

## AWS structure

| Module | Owner |
|---|---|
| `configuration.py` | Immutable settings for low-level operations. |
| `client.py` | boto3 sessions, pagination, and AWS errors. |
| `infrastructure.py` | VPC, subnet, security group, key pair, and transit gateway operations. |
| `catalog.py` | Instance type and machine image translation. |
| `servers.py` | Instance, mesh interface, and multicast registration operations. |
| `ip_addresses.py` | Elastic IP address operations. |
| `provider.py` | Contract composition and host network setup. |

### Multicast discovery

WG Mesh finds a virtual machine with IPv4 multicast on `239.1.1.1`. An AWS VPC subnet does not carry multicast, so Atlas creates one transit gateway multicast domain for the region.

Atlas registers each host statically as a multicast group member and a multicast group source. It does not use IGMPv2. WG Mesh reads discovery frames with an eBPF hook on the uplink and never joins the group with a socket, so the host sends no IGMP membership report and dynamic membership would leave the domain empty.

WG Mesh sends discovery with a multicast time to live of 1. Atlas therefore uses one subnet in one availability zone for the whole region. Every host shares that subnet.

### Host network shape

An Atlas host gets a second network interface in the Atlas subnet. That interface carries mesh traffic and is the interface that Atlas registers with the multicast domain.

AWS gives no stable guest device name, so `aws/configure-private-network.sh` finds the interface by its MAC address and renames it to `atlas-mesh`. metald uses that name as its mesh uplink.

The cloud-init network hotplug handler renames an attached interface back to its default name. Atlas launches each instance with user data that limits cloud-init network updates to the first boot, so the handler does not run.

The script sends the IPv4 multicast range through `atlas-mesh`. It writes persistent configuration for Netplan or `systemd-networkd`.

Atlas stores the mesh network interface ID before it starts the attachment. AWS deletion uses only this stored ID. It verifies the interface attachment before it deletes the interface or the instance.

### Network exposure

The Atlas security group allows all inbound traffic during development. Scaleway hosts have the same exposure because Atlas does not configure a Scaleway firewall.

### Instance types

Atlas needs hardware virtualization and two network interfaces. The catalog rejects instance types that have local instance storage.

A bare metal type provides the processor extensions. A virtual type must report `nested-virtualization` in `ProcessorInfo.SupportedFeatures`.

AWS keeps nested virtualization off until an instance asks for it, so Atlas launches a virtual instance with `CpuOptions.NestedVirtualization` set to `enabled`. A bare metal instance rejects that option, so Atlas does not send it.

`DescribeInstanceTypes` reports no price, so the catalog prices stay empty.

### Storage volumes

Atlas creates a 64 GiB `gp3` root volume and a 500 GiB `gp3` storage volume. Both volumes are part of the instance launch request.

The image metadata supplies the root device name. AWS does not give stable NVMe device names, so the provider sends the `/dev/disk/by-id` path of the storage volume. The path holds the volume ID without its dash.

On an AWS server, the Metal Server actions **Resize Root Disk** and **Resize Storage Disk** change the size, IOPS, and throughput of an EBS volume. AWS refuses a smaller size and a second change to a volume within 6 hours. When the guest sees the new size, `aws/grow-disk.sh` grows the root partition and its ext4 file system, or runs `zpool online -e` for the storage pool.

Both volumes have `DeleteOnTermination` enabled. A stop or a hardware change does not remove the storage volume, but instance deletion removes it.

### Public IPv4 addresses

AWS translates each public address to a secondary private address on the primary network interface. Atlas stores this private address as the host address.

Metal maps the host address to the virtual machine. Detach uses the stored host address, so a retry can remove the secondary address after disassociation.

Atlas does not use the primary private address for a virtual machine. This rule keeps the host public address separate from virtual machine traffic.
