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

The Create Machine Image action calls `POST /vms/{id}/snapshots`. Metal creates local staging and returns a UUIDv7. Atlas uses this UUID as the Virtual Machine Image name. A long background job then:

1. Creates separate S3 multipart uploads for `images/{image-id}/rootfs.img` and `images/{image-id}/kernel`.
2. Signs 2 GiB upload parts for 24 hours.
3. Calls `POST /snapshots/{snapshot_id}/upload`.
4. Verifies the sizes, SHA-256 values, part numbers, ETags, and final S3 object sizes.
5. Completes both uploads and deletes the local staging data.

The image record keeps the source server, multipart upload IDs, transfer status, and an error. Retry Transfer continues from these values. `source_virtual_machine` is audit text only. Metal deletes staging data after 48 hours without activity.

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

## Termination and deletion

The Terminate action asks Metal to remove the VM. Atlas keeps the request metadata. Delete the document only after Metal confirms that the VM is absent.
