# Rescue mode

Rescue mode boots a separate image and attaches the VM disk as `/dev/vdb`, unmounted. Use it to repair a guest that cannot boot or to copy files out.

## Configure the rescue image

Run `pilot frappe --site SITE build-ubuntu-rescue-image`. Run it on an amd64 host. It builds an Ubuntu 24.04 amd64 image with the repair tools, turns on **Cache Image**, and sets **Rescue Virtual Machine Image** in Atlas Settings. Each amd64 host then keeps the image, so a later boot does not depend on its download URL. See [system images](../storage/image-records.md#system-images) for how a new build affects earlier images. Until this setting has an image, `enable-rescue-mode` returns 409.

## Enter and leave

| Step | Tenant API | Result |
|---|---|---|
| Enter | `POST /actions/enable-rescue-mode`, then `POST /actions/restart` | The VM boots the rescue image. |
| Leave | `POST /actions/disable-rescue-mode`, then `POST /actions/restart` | The VM boots its own disk. Metal removes the rescue disk. |

A stop and start also applies the change. So does any other cold boot, such as a guest crash or a host restart.

`is_rescue_mode_enabled` reports what the next cold boot uses. It changes at once. Metal stores the rescue image in `config.json`.

`is_in_rescue_mode` reports whether the last boot used the rescue image. It does not change until that boot. Metal stores it in `status.json`.

A restart is pending while the two values differ. Migrations are rejected while either value is true.

Repairs write directly to the VM disk. Take a snapshot before you repair it. A snapshot always captures the VM disk, not the rescue disk, and can run before or during rescue mode.

## Repair the disk

Open the SSH console, or connect with a key that the VM already has. The TTY console shows a temporary root password for this boot in its login banner.

A terminal login opens the repair menu. The menu shows the disk and its mount state, and asks for each choice in a dialog. It unmounts or remounts the disk when a tool needs that, after it asks you. Select **Exit** to leave it, and run `atlas-rescue-menu` to open it again.

| Menu item | What it does |
|---|---|
| Mount or Unmount your VM disk | Mounts at `/mnt/original`, read-write or read-only. |
| See what is using disk space | Shows byte and inode usage. |
| See errors from the last boot | Shows error messages from the last boot of the installed system. |
| Read system logs | Shows the installed journal or recent text logs. |
| Check the disk for errors | Runs `e2fsck -n`. It makes no changes. |
| Repair disk errors | Runs `e2fsck -f -y`. It accepts every repair and can delete damaged files. |
| Expand the filesystem | Runs `e2fsck -f -p`, then `resize2fs`, after a disk resize. |
| Edit /etc/fstab | Saves a backup, opens the file in an editor, then checks it. |
| Reset a user password | Sets a new password for an installed account. |
| Add this VM's SSH keys | Adds the VM keys to an installed account. Existing keys stay. |
| Turn off the firewall | Turns off ufw, firewalld, nftables, iptables, and netfilter-persistent at the next normal boot. The firewall in Atlas does not change. |
| Open a shell inside your system | Opens a root shell in the installed system with `chroot`. |
| Open a rescue shell | Opens a root shell in the rescue system. |

Each item also works as a command. Run `atlas-rescue --help` for the list and `atlas-rescue status` for mounts and the latest job.

Filesystem checks, repairs, and growth run as systemd jobs, so they continue after a disconnect. Run `atlas-rescue wait` to follow one again. Guided changes support whole-disk ext4 only. Use the shell for other layouts.

Metal creates a new rescue disk at each boot, so files on it are lost at a restart.

## What rescue mode allows

| Allowed | Rejected with 409 |
|---|---|
| Disk resize with `PATCH /disk`. Select **Expand the filesystem** after it. | Pause |
| Restart, start, stop, and resume | Compute changes and `POST /actions/resize` |
| Snapshot of the VM disk | Migrations |
| Enable and disable rescue mode | |
| Delete | Network, public IP, SSH key, and metadata changes |
| Read routes and the console | Sleep state changes |

Metal rejects these with the code `in_rescue_mode`. It also rejects a migration while the rescue image is stored. Before that restart, the VM otherwise works as normal.

A VM in rescue mode never sleeps. Enabling rescue mode wakes a sleeping VM, and the VM stays awake until it restarts into rescue mode. Its sleep settings apply again after rescue mode ends. Metal does not give the guest its user data while the rescue image is stored.

## Upgrade and rollback

Only a VM with a stored rescue image or in rescue mode has rescue fields in its records. An older `metald` cannot read those records, so leave rescue mode before you install an older binary.

## Code

The [VM manager](../../metal/internal/vm/SPEC.md) owns the rules. The [storage package](../../metal/internal/storage/SPEC.md) owns the rescue disk. The [Firecracker runtime](../../metal/internal/firecracker/SPEC.md) reports the mode from the jail.
