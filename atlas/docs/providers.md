# Metal Server providers

Atlas uses `ServerProvider` as the server provider extension point. The registry maps one stable provider type to one provider class. Atlas has two providers: Scaleway and AWS.

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
