# storage: ZFS images and VM disks code contract

For Go code, follow the [Go review guide](../../../llm/go-code-review-guide.md). The handbook explains [image transfer](../../../docs/storage/index.md), [host storage](../../../docs/storage/host-storage.md), and the [host layout](../../../docs/storage/host-layout.md).

This package imports images, clones VM disks, manages warm artifacts, and stages Machine image uploads. Consumers define the small interfaces they need. This package exports no broad storage interface.

## Owners

| Type | Code responsibility |
| --- | --- |
| `ZFSPool` | Dataset names, device paths, and capacity. |
| `VirtualMachineStore` | VM disk preparation, growth, usage, and release. |
| `ImageStore` | Image locks, manifests, cache policy, pruning, and warm artifacts. |
| `SnapshotStore` | Snapshot locks, staging, upload, deletion, and pruning. |
| `MigrationTransfer` | ZFS snapshot transfer between hosts. |
| `Stores` | The four services from `NewStores`. |

## Invariants

- `EnsureImage` validates architecture, URLs, and SHA-256 values. One image reference cannot identify different content. A dependent VM clone keeps its source image from deletion.
- `PrepareBoot` ensures the image, kernel link, disk clone and growth, and block node in the jail. `PrepareRootFileSystem` does disk work without the kernel. `Release` promotes dependent staging clones before removing a VM disk.
- `SetImagePolicies` atomically replaces the policy file. The [image reconciler](../reconciler/SPEC.md) downloads and prunes.
- `Stage` creates a UUIDv7 snapshot ID. `StartUpload` checks signed parts, removes leftover part buffers, and starts asynchronous upload.
- The controller chooses the multipart part size, because it signs each part. Part count is an upper bound until compressed size is known. The digest covers uncompressed bytes. `UploadedArtifact` reports both raw and stored sizes.
- Staging metadata holds uploaded parts and the multipart upload ID. After restart, send only missing parts. Reuse a stored part only when this pass produced the same length. A new upload ID must not reuse an old ETag. Retry a refused part at most three times.
- Live upload progress is in memory. `UploadStatus` reports a running upload with no goroutine as pending so the controller can restart it. Shutdown cancels uploads and waits within the daemon deadline.
- `DeleteSnapshot` stops an upload, removes its staging clone before the source snapshot, and keeps data used by an active upload. A download detects zstd by frame magic, sizes the ZFS volume from the frame content size, and checks the decoded digest.
- Migration receives only one matching snapshot GUID. Its resume token must name the requested snapshot. It cannot read another dataset. `AbortReceive` removes destination data before VM records.
- One lock covers each image reference or snapshot ID. A failed multi-step create removes what it made. Deletes accept an absent resource.
- A warm key includes image identity, exact VM shape, and Firecracker compatibility. Warm memory and Firecracker state stay on the host.

## Validation

Use [image upload tests](image_upload_test.go) and [migration transfer tests](migration_transfer_test.go) for retry and cleanup changes. The [VM manager contract](../vm/SPEC.md) coordinates storage with runtime and network.
