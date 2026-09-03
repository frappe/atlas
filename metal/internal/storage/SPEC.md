# storage: ZFS images and VM disks

[internal SPEC](../SPEC.md) · overview: [docs/storage.md](../../docs/storage.md)

## Purpose

Package `storage` imports images, creates fast VM disk clones, manages warm artifacts, and stages Machine image uploads.

## Types

| Type | State and responsibility |
|---|---|
| `ZFSPool` | Pool name, dataset names, device paths, and capacity. |
| `VirtualMachineStore` | VM disk preparation, usage, growth, and release. |
| `ImageStore` | Image directory, HTTP client, image locks, manifests, policy, pruning, and warm artifacts. |
| `SnapshotStore` | Snapshot directory, HTTP client, snapshot locks, staging, upload, deletion, and pruning. |
| `Stores` | The four services created by `NewStores`. |

Consumers define the interfaces that they need. The storage package does not export one broad storage interface.

## Dataset layout

```text
ZFS
<pool>/images/<image>@ready       VM clone source
<pool>/vms/<vm-id>                VM disk
<pool>/staging/<snapshot-id>      read-only upload source
<pool>/warm/<key>@ready           warm boot disk

files
images/<image>/manifest.json
images/<image>/vmlinux
images/<image>/boot-args
images/<image>/last-used
images/<image>/warm/<key>/state
images/<image>/warm/<key>/memory
snapshots/<id>/metadata.json
snapshots/<id>/vmlinux
image-policies.json
```

## Clone and snapshot lineage

```text
image @ready -> VM clone -> temporary staging or warm source snapshot
```

VM clones share image blocks. Staging clones share one fixed VM snapshot. Warm promotion uses ZFS send and receive for an independent warm disk.

## Image import

`EnsureImage` verifies the architecture, URLs, and SHA-256 values. An image reference cannot identify different content.

Metal downloads with bounded retries, creates a ZFS volume, copies the root file system, and creates `@ready`. It stores the kernel and manifest in the image directory.

## Provision flow

`PrepareBoot` ensures the image, links the kernel, clones the VM disk when absent, grows it when required, and creates the jail block node.

`PrepareRootFileSystem` performs disk preparation without the kernel link. `Release` removes the VM dataset and its snapshots.

## Image policy

`SetImagePolicies` atomically replaces the policy file. The reconciler downloads retained images and removes non-retained images after 24 idle hours.

A successful VM start records image use. Metal keeps an image when a dependent VM clone prevents deletion.

## Snapshot and image operations

`StageSnapshot` creates a VM disk snapshot, a read-only staging clone, a staged kernel, and metadata. Snapshot IDs are UUIDv7 values from the Firecracker driver.

`UploadSnapshot` validates 2 GiB multipart ranges and streams each artifact. It returns SHA-256 values and HTTP ETag values.

`DeleteSnapshot` removes the staging clone before the source snapshot and files. `PruneStagedSnapshots` removes staging after 48 idle hours.

## Warm artifacts

The warm key includes image identity, exact VM shape, and Firecracker compatibility. `ImageStore` keeps state, memory, and an independent warm disk snapshot.

Memory and Firecracker state never leave the host. Warm artifacts are not public VM restore points.

## Chroot materialization

Metal links the kernel into the jail and creates a block node for the VM volume. `LinkOrCopy` avoids a full memory-file copy when the filesystem supports links or reflinks.

## Related

- [docs/storage.md](../../docs/storage.md) gives the broad storage model.
- [docs/snapshots.md](../../docs/snapshots.md) describes staging and warm artifacts.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) coordinates VM operations.
- [docs/host-layout.md](../../docs/host-layout.md) lists files and datasets.
