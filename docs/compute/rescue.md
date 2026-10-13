# Rescue mode

Rescue boots a separate Ubuntu image and attaches the original VM disk as `/dev/vdb`. The original disk starts unmounted. Use rescue when the installed system cannot boot, SSH access fails, the filesystem needs repair, or files must be copied before a rebuild.

The [Atlas VM bootstrap](../../scripts/atlas-vm/README.md) builds and publishes the rescue image, then selects it if Atlas Settings has no rescue image. It preserves an existing selection. For another installation, run `pilot frappe --site SITE build-ubuntu-rescue-image --storage site-file --skip-existing --set-default-if-missing`. The bench user needs sudo access to the rescue-image builder. The command rejects an ambiguous image title instead of choosing a build silently.

## Enter rescue

A VM administrator or operator selects **Rescue Mode** on the VM page. Site-scoped access does not grant a VM console or disk repair access. The VM must be running or stopped. Entry preserves that desired power state. A stopped VM stays stopped until Start is selected.

Choose one access mode:

| Mode | Original disk | Protection |
| --- | --- | --- |
| Protected | Writable | Requires a local ZFS checkpoint before the rescue guest starts. This is the default. |
| Read only | Firecracker rejects writes | No new checkpoint. Use inspection and copy-out tools. |
| Unprotected | Writable | Requires separate acknowledgement. There is no new undo baseline. |

Metal checks checkpoint capacity when accepting protected entry, before stopping the original guest, and before creating the checkpoint. A failed check does not grant unprotected writes. Select read only or explicitly acknowledge unprotected access. The check does not reserve future pool capacity.

The rescue image is pinned for the session. Stop and Start preserve its temporary disk and the selected access mode. To change access mode, exit and enter again. A retained checkpoint requires protected re-entry until it is resolved.

Object-storage download URLs expire after 24 hours. To renew expired URLs, repeat the rescue entry request through the API with the same access mode. Atlas renews URLs for the pinned image, even if the default changed.

The pinned image must still be enabled and Available in Atlas. URL renewal does not start a stopped VM.

## Connect and repair

Open the VM console or connect by SSH with a configured VM key. SSH password authentication is disabled.

The serial login banner shows a random root password for the current rescue boot. It remains available to a viewer who connects after boot. It changes on the next boot. Only root can read the banner file inside the guest.

For browser console access through a site alias, deploy the Pilot commit linked in [realtime proxy configuration](console.md#configure-the-realtime-proxy) and regenerate nginx configuration.

For an SSH public-key denial, use the serial console to run `journalctl -b --grep=atlas-ssh-metadata`. Metadata failures report the token, key-list, or key request stage and the exception type. HTTP failures also report the status code. Tokens, keys, response bodies, and exception text are not logged. Successful requests and an empty key list are silent. The shared helper supplies these diagnostics in both normal and rescue images.

Interactive serial login opens the repair menu. `atlas-rescue-menu` opens it again. `atlas-rescue status` reports disk access, mounts, and the latest managed operation. The CLI and menu use the same tools.

| Tool | Operation |
| --- | --- |
| Mount | Mount the original disk at `/mnt/original`. Read-only mounts use `ro,noload` to avoid journal replay. |
| Filesystem check | Inspect with `atlas-rescue fsck`. Use `--repair` to request automatic safe ext4 repairs. The disk must be unmounted. A failed mount inspection blocks the operation. |
| Chroot | Bind runtime filesystems and open the installed system shell. Bind mounts are removed when the shell exits. |
| Linux password reset | Run `atlas-rescue passwd USER`. A locked account requires an explicit `--unlock`. This does not reset a Frappe password or change SSH policy. |
| SSH keys | Add current VM keys to an explicitly selected installed account. Existing keys remain. |
| Logs | Read the installed persistent journal or recent text logs. |
| Filesystem growth | Run `atlas-rescue resize` on an unmounted ext4 disk after its block device has already grown. |
| Disk usage | Inspect both byte and inode usage. |
| Copy out | Discover Frappe sites and copy recovery material to a selected destination. |
| Expert shell | Run manual recovery commands with root access. |
| Exit | Keep disk changes and return to the original system. Use Atlas for rollback. |

Run `atlas-rescue umount` before switching between writable repair tools and read-only logs, disk usage, or copy-out. Guided tools refuse an existing mount with a different access mode.

Guided modification supports standard Atlas whole-disk ext4 volumes. Other layouts require manual inspection. LVM, encryption, RAID, partitioned disks, XFS, and Btrfs do not have guided repair workflows.

Filesystem checks, growth, and copy-out run as managed systemd jobs. They survive terminal disconnects. Reconnect and run `atlas-rescue status` for the log and result. Managed tools serialize their operations. An arbitrary command in the expert shell can bypass that coordination. Do not reboot or change mounts during an active operation.

Password input is hidden and passed to the password tool over standard input. Passwords are not placed in command arguments or operation logs. Password reset refuses accounts with a non-login shell.

## Copy out Frappe data

Run `atlas-rescue copy-out` to inspect the offline inventory. Discovery reads paths without executing installed application code. It identifies site configuration, public and private files, backups, app code, bench configuration, migration artifacts, and standard local database directories.

Run `atlas-rescue copy-out --destination PATH` to start a managed export. The destination must be empty and outside the original disk. Use a separate mounted disk or remote filesystem, or pull files directly with SSH from a workstation. Files stored on the rescue disk are temporary and disappear on exit.

The private export directory includes a manifest with present and missing components, file sizes, and SHA-256 hashes for regular files. It lists symbolic links and special files separately. It does not read FIFOs or device nodes to compute hashes. Export refuses a source whose parent links escape the original filesystem. An interrupted export remains marked incomplete. Configuration files can contain credentials. Keep the export private.

Raw database files are preservation copies, not validated portable backups. External databases, external volumes, and nonstandard data locations require separate recovery. Rescue does not start a database, repair a Frappe site, run migrations, or change a site's configuration automatically.

## Keep, undo, and finish

**Keep changes** returns to a cold boot of the original disk and retains the checkpoint for 12 hours. The deadline starts when Metal applies the first keep exit. The checkpoint includes the entire original disk. Rolling back also discards writes made after returning to normal boot.

Re-entering rescue during this window reuses the first checkpoint and its original deadline. If the deadline passes during an active rescue session, protection remains until that session ends. Keep changes then deletes the expired checkpoint. Rollback remains available while that active session continues.

**Roll back** stops the guest, verifies the checkpoint identity, restores the original disk, records restore completion, and removes the checkpoint. It then cold boots only if the desired power state is running. A missing or replaced checkpoint is an error. A restore failure leaves the guest stopped for retry. It never becomes a keep exit.

**Finish recovery** deletes a retained checkpoint early and keeps disk changes. Automatic expiry does the same. Expiry never rolls back a disk. An accepted disposition is durable; a competing request cannot replace an unfinished action. Rollback and finish requests include `recovery_generation` from the displayed checkpoint. Metal rejects a confirmation for another session.

The local checkpoint is not an independent backup. It cannot undo external database changes, email, payment operations, network configuration outside the disk, or loss of the storage pool. Copy important data off the host before risky repair.

## Restrictions and failures

Migration, snapshots, and CPU, memory, or disk resizing remain blocked until recovery is complete. Automatic idle sleep is disabled while rescue is selected. Entry, exit, and rollback invalidate saved guest memory.

The dialog reports the requested and observed rescue state, access mode, recorded checkpoint, expiry, and operation errors. If a state read fails, polling stops and actions stay disabled. Select **Retry status** to read the state again and resume polling. An accepted request can fail while downloading an image, stopping the guest, preparing storage, or booting. Correct the cause and retry the same action. Do not substitute a different exit action after a rollback failure.

A normal rescue reboot sends a durable keep-exit notification through vsock. A forced reset that bypasses the hook preserves rescue. If notification fails, use Keep changes in Atlas. Host-directed Stop preserves the rescue selection.

## Code and validation

The [VM manager](../../metal/internal/vm/SPEC.md) owns the durable recovery record under the VM operation lock. The [storage package](../../metal/internal/storage/SPEC.md) owns tagged ZFS checkpoints and GUID verification. The [Firecracker runtime](../../metal/internal/firecracker/SPEC.md) owns drive access, cold boot, console, and guest reboot intent. [Atlas](../../atlas/vm/core/vm_service.py) owns image selection and controller calls.

Validation must cover real ZFS rollback with closed disk descriptors, read-only enforcement inside Firecracker, both power states, keep and rollback retries, retained re-entry, active expiry, lost checkpoints, SSH access, password reset, and managed jobs after disconnect. Unit tests alone cannot establish guest and storage behavior.

Use the expert shell for network and cloud-init repair.
