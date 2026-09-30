# Repair a VM in rescue mode

Rescue mode boots a repair system in the same VM. The original disk is attached as a second writable drive. Its filesystem stays unmounted until you mount it. Repairs to that disk persist after rescue ends.

## Prepare the image

Build an Ubuntu 24.04 rescue image. The builder includes filesystem tools, SSH access through the VM's keys, and a reboot notification hook.

```sh
pilot --site SITE build-ubuntu-base-image --version 24.04 --rescue
```

In Atlas Settings, select **Rescue Virtual Machine Image**. It must be an enabled, Available System image without a memory snapshot. Its architecture must match the VM. The supplied builder supports amd64.

Use an image made with `--rescue`. Atlas validates the image record, but cannot inspect whether a custom image contains the tools and reboot hook. The guest kernel must support virtio-vsock. The builder installs an Ubuntu kernel with its matching modules and loads `vmw_vsock_virtio_transport`. Package installation uses the configured Ubuntu repositories. Published image hashes identify the resulting artifacts.

The rescue image disables Snap services. Snap initialization can wait for Internet access and delay cloud-init. The installed repair tools and SSH access through metadata work without Internet access.

Each session keeps its selected image. Changing the setting affects new sessions. Repeating an enable request does not replace an active session.

## Enter and use rescue

Open the VM's **Actions > Rescue Mode** dialog, then select **Enter Rescue**. Tenant clients can send `PUT /api/atlas/virtual-machines/{id}/rescue` with `{"enabled": true}`.

The dialog remains available when the VM reports a failed state, so you can exit after a failed rescue boot. Metal still validates each requested transition. The action is unavailable during migration.

| Current power state | Result |
| --- | --- |
| Running | Metal stops the guest and boots rescue. |
| Stopped | Metal selects rescue and leaves the VM stopped. Start boots rescue. |
| Paused | Stop the VM before changing rescue mode. |

Connect with a configured VM SSH key. The rescue image uses its own initialization. It does not run the original VM user data. The TTY console shows boot output; SSH supplies the authenticated repair shell.

Inspect the original disk before mounting it:

```sh
sudo lsblk -f /dev/vdb
sudo mkdir -p /mnt/original
# Use /dev/vdb for a filesystem on the whole disk.
# Use its filesystem partition instead if the disk has partitions.
sudo mount /dev/vdb /mnt/original
```

Unmount a filesystem before an offline repair such as `fsck`. Rescue does not mount or repair the disk automatically.

## Keep or end the session

| Action | Result |
| --- | --- |
| Stop, then Start | Keep rescue selection and the rescue disk. RAM and running processes are lost. |
| Crash or host restart | Keep rescue selection and the rescue disk. |
| Atlas Reboot | End rescue and boot the original disk. |
| Ordinary `sudo reboot` in rescue | Report reboot intent to Metal, then end rescue and boot the original disk. |
| Exit Rescue while running | Stop rescue, delete its detached disk, and boot the original disk. |
| Exit Rescue while stopped | Delete the rescue disk and keep the VM stopped. |

Copy any needed notes or tools off the rescue disk before leaving. A new session starts from a fresh image clone. The original disk is not discarded.

Metal must receive reboot intent before the guest stops. A forced reset that bypasses the hook preserves rescue. A failed notification can also preserve rescue. Use Exit Rescue if needed. A recorded intent remains authoritative if the acknowledgement is lost.

## Restrictions and limits

Automatic idle sleep is disabled while rescue is selected. Migration, snapshots, and CPU, memory, or disk resizing are blocked. Exit rescue and wait for the change to complete before those operations.

Each drive receives half the configured disk throughput and IOPS limit. A zero limit remains unlimited. For an odd IOPS limit, each drive uses a two-second refill period to preserve the exact average. Initial boot and live limit changes use the same split.

The VM detail response includes `rescue.enabled`, `observed_enabled`, `pending`, and `image_ref`. Metal exposes the corresponding desired and observed rescue generations. An accepted request can still fail during image download, shutdown, storage work, or boot. Inspect the operation error and host logs. Failed boots retain rescue selection and the session disk for retry.

## Reboot notification

The rescue image uses systemd's final shutdown hook. Only the `reboot` action sends a notification. It connects through Firecracker vsock to host CID 2, port 187. The host listener belongs to that VM's jail.

Metal writes the intent before sending `ok`. Its record contains a guest boot ID, rescue generation, and Linux host boot ID. A daemon restart can adopt the record and listener. A different host boot or rescue session cannot reuse the intent. A host-directed Stop closes the listener and removes intent before it signals the guest.

Metal controls boot selection locally. Lifecycle logs are diagnostic output. No telemetry service is required for rescue transitions. This branch emits structured logs; the separate Datum exporter work must be integrated before those events reach ClickHouse.

See the [Firecracker vsock protocol](https://github.com/firecracker-microvm/firecracker/blob/v1.16.1/docs/vsock.md) and [systemd shutdown hooks](https://www.freedesktop.org/software/systemd/man/254/systemd-halt.service.html).

## Code and validation

- [VM manager](../../metal/internal/vm/SPEC.md) owns desired selection, restrictions, and stop-before-delete ordering.
- [Firecracker runtime](../../metal/internal/firecracker/SPEC.md) owns cold boot and reboot intent.
- [Storage](../../metal/internal/storage/SPEC.md) owns the session clone in `<pool>/rescue/<vm>`.
- [Atlas VM service](../../atlas/vm/core/vm_service.py) owns image selection and controller calls.
- [Rescue tests](../../metal/internal/vm/rescue_test.go) cover power behavior, retention, cleanup retry, and snapshot restrictions. [Listener tests](../../metal/internal/firecracker/rescue_test.go) cover intent lifetime.

A live validation must check SSH access, the unmounted original disk, repair persistence, Stop/Start, both reboot paths, and daemon and host restarts. Unit tests cannot establish these guest and ZFS behaviors.
