# Virtual machines

Atlas stores VM request metadata. Metal stores VM runtime state and desired state.

## Creation

Atlas creates and commits a draft to reserve a stable VM name. This name is the Metal VM ID.

Atlas signs image URLs and sends one idempotent `PUT /vms/{name}` request. The request contains one image object. Both System images and Machine images use their own rootfs and kernel objects. Metal stores the intent and returns HTTP `202`. Atlas does not wait for startup.

If Atlas loses the response, it sends `GET /vms/{name}`. Atlas finalizes a confirmed VM and keeps an uncertain draft. Atlas deletes a draft only after Metal returns HTTP `404`.

## Placement

Atlas uses the latest host capacity sample. The host and image architectures must match. The host must have enough CPU, memory, and storage.

Atlas sends WireGuard peers and desired cached images with `POST /sync`. It receives host capacity in the same exchange.

## Images

Virtual Machine Image is the durable boot artifact. `image_type` is `System` or `Machine`. Each image has its own rootfs and kernel object key, exact byte size, and SHA-256 value. The immutable reference uses the architecture and both artifact hashes.

Only enabled, Available images can create VMs. Atlas sends enabled, Available images with `cache_image` to each host through `POST /sync`. Signed URLs are valid for 24 hours.

The policy is:

| Cache Image | Memory Snapshot | Host behavior |
|---|---|---|
| No | No | Download on demand. |
| Yes | No | Retain the rootfs and kernel. |
| No | Yes | Use a normal cold boot. |
| Yes | Yes | Retain the artifacts and build a local warm snapshot. |

Atlas never uploads memory or Firecracker state to S3. A memory snapshot needs an explicit CPU, memory, and disk configuration. Metal boots a local template for about 5 minutes. It keeps the disk, state, and memory on that host. Only an exact VM shape can use these artifacts.

## Machine image transfer

The Create Machine Image action calls `POST /vms/{id}/snapshots`. Metal returns a UUIDv7 for local staging. Atlas uses it as the Virtual Machine Image name. A background job then:

1. Creates separate S3 multipart uploads for `images/{image-id}/rootfs.img` and `images/{image-id}/kernel`.
2. Signs 2 GiB upload parts for 24 hours.
3. Calls `POST /snapshots/{snapshot_id}/upload`.
4. Verifies the sizes, SHA-256 values, part numbers, ETags, and final S3 object sizes.
5. Completes both uploads and deletes the local staging data.

The image record keeps the source server, upload IDs, status, and errors. Retry Transfer uses these values. `source_virtual_machine` is audit text only. Metal deletes staging after 48 hours without activity.

## System image publisher

Build and publish a pinned Ubuntu image:

```sh
bench --site <site> atlas build-ubuntu-base-image --version 24.04 --architecture amd64
```

The publisher creates an Available System image and stores exact artifact sizes.

## SSH keys

Atlas can replace the complete VM SSH key list without storing it in the Virtual Machine DocType. Metal saves the list and updates MMDS for an active VM. The base image `AuthorizedKeysCommand` reads the current MMDS keys during each login.

## Custom metadata

Atlas stores custom VM metadata as key-value rows and sends it to Metal. Guests read each value from `latest/meta-data/attributes/<key>`.

The Ubuntu image builder installs the Atlas cloud-init datasource. See [`build_ubuntu_server_image.sh`](scripts/build_ubuntu_server_image.sh).

## Network changes

Metal owns the VM network state. Atlas reads it, changes one setting, and sends all mutable settings with `PUT /vms/{name}/network`. It stops if Metal does not return the current state.

| Action | Behavior |
|---|---|
| Attach IP Address | Sets an attach intent and sends the address with `uplink` egress. |
| Detach IP Address | Sends an empty address and sets a detach intent. |
| Edit Network Throughput | Sends private and public limits in MiB/s. `0` removes a limit. |
| Change Egress Mode | Sends `uplink`, `mesh`, or `none`. |

Atlas applies the Metal change before it releases an address. A VM can hold one public IPv4 address. Public IPv4, egress, and throughput limits can also be set during creation.

Egress controls internet reachability. It does not control mesh reachability.

| Mode | VM can reach | Public IPv4 | Throughput limits |
|---|---|---|---|
| `uplink` | mesh peers and the internet | allowed | private and public |
| `mesh` | mesh peers only | rejected | private applied, public stored |
| `none` | nothing | rejected | none |

A public IPv4 address needs `uplink`. Atlas refuses `mesh` and `none` while an address is attached.

Active connections can stop when the public IPv4 address or the egress mode changes.

## Disk limits

The VM configuration can set `disk_throughput_mibps` and `disk_iops`. Each limit covers reads and writes together. A value of `0` does not apply a limit.

The Edit Disk Limits action sends both values with `PUT /vms/{name}/disk`. Metal sets a Firecracker drive rate limiter, so a change needs no VM restart.

## Termination and deletion

The Terminate action asks Metal to remove the VM. Atlas keeps the request metadata. Delete the document only after Metal confirms that the VM is absent.
