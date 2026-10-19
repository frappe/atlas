# Rescue mode

Rescue boots a separate Ubuntu image and attaches the original VM disk as `/dev/vdb`. The original disk starts unmounted. Use rescue when the installed system cannot boot, SSH access fails, the filesystem needs repair, or files must be copied before a rebuild.

The [Atlas VM bootstrap](../../scripts/atlas-vm/README.md) builds and publishes a normal System image tagged `purpose=rescue`. On entry, Atlas selects the newest enabled, Available rescue image for the VM architecture and sends its reference to Metal. An active session keeps the selected image.

For another installation, run `pilot frappe --site SITE build-ubuntu-rescue-image --storage site-file --skip-existing`. The bench user needs sudo access to the rescue-image builder.

## Enter rescue

A VM administrator or operator selects **Rescue Mode** on the VM page. Site-scoped access does not grant a VM console or disk repair access. The VM must be running or stopped. Entry preserves that desired power state. A stopped VM stays stopped until Start is selected.

Take an ordinary VM snapshot before making repairs. Rescue does not create a checkpoint or provide undo. Repairs write directly to the original disk.

The rescue image and its boot disk remain selected across reboot, Stop/Start, and host restart. Use **Exit rescue** to return to the original disk. Exit preserves the desired power state and deletes the rescue boot disk. Copy files stored on that disk elsewhere before exit.

Object-storage download URLs expire after 24 hours. To renew expired URLs, repeat the rescue entry request through the API. Atlas renews URLs for the pinned image, even if a newer rescue image is available.

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
| Exit | Unmount the original disk and show how to exit through Atlas. Reboot remains in rescue. |

Run `atlas-rescue umount` before switching between writable repair tools and read-only logs, disk usage, or copy-out. Guided tools refuse an existing mount with a different access mode.

Guided modification supports standard Atlas whole-disk ext4 volumes. Other layouts require manual inspection. LVM, encryption, RAID, partitioned disks, XFS, and Btrfs do not have guided repair workflows.

Filesystem checks, growth, and copy-out run as managed systemd jobs. They survive terminal disconnects. Reconnect and run `atlas-rescue status` for the log and result. Managed tools serialize their operations. An arbitrary command in the expert shell can bypass that coordination. Do not reboot or change mounts during an active operation.

Password input is hidden and passed to the password tool over standard input. Passwords are not placed in command arguments or operation logs. Password reset refuses accounts with a non-login shell.

## Copy out Frappe data

Run `atlas-rescue copy-out` to inspect the offline inventory. Discovery reads paths without executing installed application code. It identifies site configuration, public and private files, backups, app code, bench configuration, migration artifacts, and standard local database directories.

Run `atlas-rescue copy-out --destination PATH` to start a managed export. The destination must be empty and outside the original disk. Use a separate mounted disk or remote filesystem, or pull files directly with SSH from a workstation. Files stored on the rescue disk are temporary and disappear on exit.

The private export directory includes a manifest with present and missing components, file sizes, and SHA-256 hashes for regular files. It lists symbolic links and special files separately. It does not read FIFOs or device nodes to compute hashes. Export refuses a source whose parent links escape the original filesystem. An interrupted export remains marked incomplete. Configuration files can contain credentials. Keep the export private.

Raw database files are preservation copies, not validated portable backups. External databases, external volumes, and nonstandard data locations require separate recovery. Rescue does not start a database, repair a Frappe site, run migrations, or change a site's configuration automatically.

## Restrictions and failures

Migration, snapshots, and CPU, memory, or disk resizing remain blocked until rescue exit is applied. Automatic idle sleep is disabled while rescue is selected. Entry and exit invalidate saved guest memory.

The dialog reports the selected mode, host state, and operation errors. If a state read fails, polling stops and actions stay disabled. Select **Retry status** to reconnect. An accepted request can fail while downloading an image, stopping the guest, preparing storage, or booting. Correct the cause and retry the action.

## Upgrade and recovery

Metal records the `recognize_rescue_state` patch before it loads VM state. Existing VM records need no conversion. Once this build writes state, older binaries can reject its rescue fields, including fields on VMs that have not entered rescue.

Use the normal Metal upgrade action. If the candidate has an unapplied patch, the upgrade script keeps that binary when its readiness checks fail. Correct the failure and retry, or install a compatible fixed build. Do not manually reinstall a binary from before the rescue patch. Exiting rescue does not make the stored records compatible with that binary.

This release recovery rule does not undo guest disk repairs. Use ordinary backups or snapshots for guest data recovery.

## Code and validation

The [VM manager](../../metal/internal/vm/SPEC.md) owns rescue selection and reconciliation under the VM operation lock. The [storage package](../../metal/internal/storage/SPEC.md) prepares the rescue boot disk through the normal image provisioning path and attaches the original disk. The [Firecracker runtime](../../metal/internal/firecracker/SPEC.md) owns cold boot and console access. [Atlas](../../atlas/vm/core/vm_service.py) selects the image and sends controller requests.

Validation covers entry and exit in both power states, reboot, Stop/Start, cleanup retries, image URL renewal, original-disk preservation, console access, and repair tools. Unit tests do not establish guest and ZFS behavior.

Use the expert shell for network and cloud-init repair.
