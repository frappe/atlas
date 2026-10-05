# Metal Server providers

Choose the provider configured for your region.

| Provider | Host model |
| --- | --- |
| [Generic](#generic-provider) | Register hosts you prepare. |
| [Scaleway](#scaleway) | Provider-managed Elastic Metal hosts. |
| [AWS](#aws) | EC2 hosts with one network interface and an Elastic IP. |
| [Redfish](#redfish) | Register an existing machine through its Redfish endpoint. |

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
4. Review the host facts. Set the provider server ID and create the Metal Server.

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

## Redfish

Redfish is registered as a server provider and appears in Atlas Settings. The automated setup input accepts `Redfish` with no provider-specific fields.

Redfish manages existing machines. Atlas Settings refuses **Metal Auto-spawn Config > Enabled**. Settings validation does not contact a baseboard management controller (BMC) or check the per-server credentials.

### Validate credentials

`RedfishProvider.validate_credentials()` checks every active Metal Server with a Redfish provider ID. It uses the saved URL, username, and decrypted password to read the registered ComputerSystem. Deleted records and other providers are excluded.

The method returns `True` only when every system is accessible and its response identifies the registered URL. It raises `RedfishError` on the first failed check or when no Redfish server is registered. Blank credentials are accepted only when the system endpoint allows anonymous access.

Validation sends GET requests only. It does not poll, change power, update observations, or save documents. Successful validation proves read access to the system. It does not prove permission to send power requests. Saving Atlas Settings does not run this check because Redfish credentials belong to Metal Servers.

To check the local registration, open a console from the Pilot bench:

```sh
pilot --site atlas-bare.localhost console
```

Then run:

```python
frappe.get_single("Atlas Settings").server_provider_controller.validate_credentials()
```

The local sample can run with authentication disabled. In that mode, it cannot reject a wrong password. A local check with a temporary authenticated Sushy instance accepted the saved valid credentials and rejected wrong or missing credentials with HTTP 401. The check rolled back its temporary credentials and left the original registration unchanged.

### Register a machine

Select **Redfish** as **Server Provider** in Atlas Settings. Keep **Metal Auto-spawn Config > Enabled** off. Sign in with the **System Manager** role.

Open the Metal Server list and select **Register Server**. Registration uses one Metal Server form. Enter the connection values and select **Register**:

| Field | Value |
| --- | --- |
| Redfish URL | Service root, Systems collection with one member, or individual ComputerSystem URL. |
| Redfish Username | BMC account name. Leave empty for an endpoint that accepts requests without authentication. |
| Redfish Password | BMC account password. Leave empty only when the username is also empty. |

For the local sample endpoint, set **Redfish URL** to `http://127.0.0.1:18000/redfish/v1/Systems`. Supply the configured credentials when authentication is enabled. Leave both credential fields empty only for anonymous access.

The endpoint must be reachable from the Atlas process. `127.0.0.1` refers to the host where Atlas runs.

The local sample identifies the same system at `http://127.0.0.1:18000/redfish/v1/Systems/5b5fbab3-d07c-4b27-b551-fbdb78c77912`. Use an individual system URL when the collection has zero or multiple members.

Atlas follows the advertised service links with GET requests. It accepts only links on the configured service and refuses redirects. HTTPS uses certificate verification. A connection, authentication, or response error prevents registration.

Registration stores the canonical system URL in `redfish_url`, credentials in `redfish_username` and the encrypted `redfish_password` field, and the system Id, Name, and UUID in `provider_metadata`. Atlas generates `provider_server_id` from the canonical system URL.

An active record with the same provider server ID is reused without changing its credentials. Registration opens that record. A Deleted record does not block a new registration.

The record stays **Pending** with provisioning incomplete. Registration does not create a machine, change BMC state, install software, prepare disks, or enqueue provisioning.

Size and image are optional for Redfish and remain required for the other providers. Registration does not establish hardware inventory or VM capacity.

### Read power state

Open or reload a saved Redfish Metal Server. Atlas reads the registered ComputerSystem with its saved credentials. The form shows **Redfish Power State** and **Redfish Health** as virtual fields.

The two fields share one GET response for that document in the current request. A new request reads the BMC again. Atlas does not store these values in the database or poll in the background.

An unavailable or unauthorized BMC leaves both fields blank and shows the read error. Atlas does not display a stale database observation. New, Deleted, unregistered, and other-provider records do not trigger a Redfish read.

`On` means that the BMC reports power on. It does not confirm that the operating system has booted or that Metal is available. Loading virtual fields does not change the Metal Server lifecycle.

Select **Actions > Refresh Power State** to read the BMC and reconcile the lifecycle. Off marks the record Stopped. An On observation changes a Stopped lifecycle to Pending. Refresh never promotes a host to Running. A failed refresh does not change lifecycle status.

Virtual reads require the System Manager role and document read permission. Atlas checks the saved system URL against the provider ID before contacting the BMC. Explicit refresh and power actions reject Deleted records and active provisioning jobs.

A per-server Redis lock serializes explicit refresh and power actions. Provider requests run without a database lock. Reconciliation then locks the saved row, reloads it, and rechecks the document guard before it writes lifecycle status. Lock identities include the site database name.

### Power on

Select **Actions > Power On** when the displayed power state is Off. Atlas reads the current state before it sends a request. If the machine is already On, Atlas reconciles the lifecycle without sending a reset.

Atlas sends `ResetType: On` to the advertised ComputerSystem.Reset target. It uses `ForceOn` only when On is absent and ForceOn is advertised. Supported values come from the inline allowable-values annotation or the advertised ActionInfo resource.

After the request, Atlas waits for the BMC to report On. An unprovisioned host stays Pending. Power on does not install Metal or prove operating system readiness.

### Power off

Select **Actions > Power Off** when the displayed power state is On, then confirm graceful shutdown. Atlas reads the current state and advertised reset capability first. If the machine is already Off, it returns without sending a reset.

Atlas sends one `GracefulShutdown` request to the advertised reset target. It returns when the BMC accepts the request with HTTP 200, 202, or 204. It does not poll power state or a task monitor. It refuses a controller that advertises only ForceOff and never falls back to forced shutdown.

The form shows **Shutdown request accepted.** Acceptance does not confirm that shutdown has finished. The displayed fields and lifecycle remain unchanged. Reload the form to read the current virtual fields, or select **Actions > Refresh Power State** to read and reconcile lifecycle status. Only explicit reconciliation marks an Off record Stopped.

The shutdown button calls `atlas.metal_server.doctype.metal_server.metal_server.poweroff_redfish_server` with the saved record name. The response does not serialize the document or evaluate its virtual fields. It requires document write permission and the same power-action guards.

### Reboot

Select **Actions > Reboot** when the displayed power state is On, then confirm the request. Atlas reads the current state and requires On. It sends the advertised `GracefulRestart` value. It refuses ForceRestart-only controllers and does not reboot an Off or transitioning machine.

Reboot always sends a new request, even when the BMC reports On. It is not idempotent. Do not repeat a request after an unknown outcome without checking the machine first.

Atlas waits for an asynchronous task to complete, when present, then observes On. A synchronous acceptance followed by On does not prove that the operating system has finished rebooting. The BMC can report On throughout a restart. Check the operating system or Metal separately before using the host.

After reboot acceptance and an On observation, Atlas changes a previously Running lifecycle to Pending because the previous readiness check is no longer valid. An unprovisioned host stays Pending. Power actions never mark a host Running.

### Power request completion and recovery

Explicit refresh and power requests use the same per-server Redis lock. A request reloads the saved record and checks permissions and lifecycle state while it holds the lock. The provider reads credentials from the saved record.

For Power On and Reboot, HTTP 200 or 204 accepts a synchronous reset. HTTP 202 requires a same-service Location task monitor. Atlas polls that monitor before checking power. A failed or cancelled task fails the action.

Reset targets, ActionInfo links, and task monitors cannot move to another service. Redirects are refused.

HTTP requests have a 10-second timeout. Power On and Reboot share a 120-second budget for task and power polling. Power Off returns after request acceptance without polling.

If a reset request times out, the action outcome can be unknown. Atlas does not repeat the POST automatically. Select **Actions > Refresh Power State** before deciding whether to send another action.

Power actions require a stable On or Off state. During a transition, refresh the observed state and wait for it to settle. A failed action does not save an assumed power state or promote the lifecycle to Running.

### Implementation and validation

The [Redfish provider](../../atlas/atlas/core/server_providers/redfish/provider.py) implements the shared `ServerProvider` contract. It owns discovery through `import_server`, registration validation through `validate_server`, and power observations through `read_power_status`. Its [client](../../atlas/atlas/core/server_providers/redfish/client.py) owns HTTP transport and Redfish resource validation.

[Provider registration](../../atlas/metal_server/core/provider_registration.py) owns local insertion and duplicate detection after provider discovery completes. It uses the provider server ID to identify and lock a registration.

[Metal Server](../../atlas/metal_server/doctype/metal_server/metal_server.py) owns the virtual fields and their observation cache for the current request. [Server power](../../atlas/metal_server/core/server_power.py) owns power orchestration and lifecycle reconciliation. Both use the shared provider interface.

The form uses the POST API `atlas.metal_server.doctype.metal_server.metal_server.register_redfish_server`. It requires the System Manager role and accepts `redfish_url`, `redfish_username`, and `redfish_password`. Direct insertion of a new Redfish Metal Server through the standard Save API is refused.

Deterministic tests cover discovery, credentials, malformed responses, unsafe links, duplicate registration, permissions, and provisioning suppression. Live checks against the local sample cover root, collection, and individual system URLs, existing-record reuse, and a fresh insert rolled back after password encryption checks. The checks compare BMC responses before and after registration.

Power-read tests cover response validation, saved credentials, changed identities, permission checks, lifecycle reconciliation, and failed reads. Virtual-field tests cover one shared read, fresh values on a new request, blank fields on failure, permission checks, and exclusion from database writes.

The Node form tests cover Refresh Power State, the single Register form, read errors, and Deleted records. Live checks matched both virtual fields to Redfish with one GET and confirmed that the database fields and lifecycle did not change. An authenticated local instance also verified the saved credential path.

Power-on tests cover idempotent On requests, ForceOn selection, ActionInfo, task monitors, failed tasks, state timeouts, and unknown POST outcomes.

A live check powered the sample from Off to On through the Atlas document method, confirmed its state with Redfish and a read-only libvirt query, and verified that a repeated Power On sent no POST.

The sample uses synchronous HTTP 204 responses. Asynchronous task behavior is covered with mocks.

Power-off tests cover HTTP 200, 202, and 204 acceptance without power or task polling, already-Off requests, ForceOff-only controllers, unknown POST outcomes, and no field writes. Form tests cover confirmation, the acceptance message, and a shutdown response without a document reload.

A live check submitted one GracefulShutdown request through the shutdown endpoint. It made one preflight GET and one reset POST, with no document serialization, later reads, or task polling. Lifecycle status did not change.

Reboot tests cover On requests, Off and transitional-state refusal, ForceRestart-only controllers, unknown outcomes, asynchronous completion, readiness invalidation, and lock commit order. The form tests cover confirmation and action visibility.

A live check sent one GracefulRestart request through the Atlas document method. The sample remained On and Pending. A read-only libvirt event listener observed the guest reboot. This confirms the sample handled the request. It does not validate Metal readiness or every BMC implementation.

Infrastructure setup, catalog discovery, and host preparation still raise `UnsupportedProviderOperation`. A Redfish region cannot complete setup. Saved Redfish forms hide unsupported host preparation actions.

The [provider package](../../atlas/atlas/core/server_providers/redfish/provider.py) implements the `ServerProvider` contract. Optional operations use the unsupported-operation behavior from the base class.

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
| Optional host import | Fill an unsaved Metal Server from an existing provider host. |
| Optional power observations | Return typed power state and health. |
| Optional public IPs | Reserve, attach, detach, and delete provider resources. |

Optional operations raise `UnsupportedProviderOperation` when the provider does not support them.

`validate_server` checks the Metal Server values that a provider needs. The default accepts every Metal Server.

Set `is_registration_only = True` when the provider manages existing hosts without Atlas provisioning. Registration calls `import_server` before it locks and inserts the local record. These hosts do not require size or image catalogs and do not enqueue provisioning.

`read_power_status` returns `ServerPowerStatus` with `power_state` and `health`. An observation does not prove Metal readiness. The Metal Server owner decides when to reconcile lifecycle status.

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
