#!/bin/bash
# Provision one Firecracker VM on this server. Single task: prepares disk,
# config, networking, then starts the systemd unit. Run once per VM.
#
# Inputs (environment variables):
#   VIRTUAL_MACHINE_NAME  - UUID, used for directory, tap, systemd instance
#   IMAGE_NAME            - directory under /var/lib/atlas/images
#   KERNEL_FILENAME       - filename inside the image directory
#   ROOTFS_FILENAME       - filename inside the image directory
#   VCPUS                 - integer
#   MEMORY_MB             - integer
#   DISK_GB               - integer, final rootfs size for this VM
#   MAC_ADDRESS           - e.g. 06:00:01:02:03:04
#   TAP_DEVICE            - e.g. atlas-<first 10 chars of vm name>
#   VIRTUAL_MACHINE_IPV6  - the VM's address inside the server's /124
#   SSH_PUBLIC_KEY        - injected into the rootfs

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?required}"
: "${IMAGE_NAME:?required}"
: "${KERNEL_FILENAME:?required}"
: "${ROOTFS_FILENAME:?required}"
: "${VCPUS:?required}"
: "${MEMORY_MB:?required}"
: "${DISK_GB:?required}"
: "${MAC_ADDRESS:?required}"
: "${TAP_DEVICE:?required}"
: "${VIRTUAL_MACHINE_IPV6:?required}"
: "${SSH_PUBLIC_KEY:?required}"

image_directory="/var/lib/atlas/images/${IMAGE_NAME}"
vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"

# 0. Verify image present. Fail loud with an actionable message so the operator
#    knows to click Sync to Server before retrying. (Image sync is multi-minute
#    and is intentionally not auto-triggered from provision.)
if [ ! -f "${image_directory}/${ROOTFS_FILENAME}" ]; then
    echo "image '${IMAGE_NAME}' not present on server (missing ${image_directory}/${ROOTFS_FILENAME}); run Sync to Server first" >&2
    exit 1
fi

sudo install -d -m 0700 "$vm_directory"
sudo install -d -m 0700 "${vm_directory}/log"

# 1. Per-VM rootfs: copy and resize.
rootfs_path="${vm_directory}/rootfs.ext4"
if [ ! -f "$rootfs_path" ]; then
    sudo cp "${image_directory}/${ROOTFS_FILENAME}" "${rootfs_path}.part"
    sudo truncate -s "${DISK_GB}G" "${rootfs_path}.part"
    sudo e2fsck -fy "${rootfs_path}.part" >/dev/null 2>&1 || true
    sudo resize2fs "${rootfs_path}.part" >/dev/null
    sudo mv "${rootfs_path}.part" "$rootfs_path"
fi

# 2. Inject SSH key, per-VM network env, hostname, swap.
mount_point="$(sudo mktemp -d /tmp/atlas-mount-XXXXXX)"
sudo mount -o loop "$rootfs_path" "$mount_point"
trap 'sudo umount "$mount_point" 2>/dev/null || true; sudo rmdir "$mount_point" 2>/dev/null || true' EXIT

sudo install -d -m 0700 "${mount_point}/root/.ssh"
printf '%s\n' "$SSH_PUBLIC_KEY" | sudo install -m 0600 /dev/stdin "${mount_point}/root/.ssh/authorized_keys"

sudo install -m 0644 /dev/stdin "${mount_point}/etc/atlas-network.env" <<EOF
VIRTUAL_MACHINE_IPV6=${VIRTUAL_MACHINE_IPV6}
EOF

# 2a. Per-VM hostname. The VM UUID is stable forever (see
#     spec/05-virtual-machine-lifecycle.md#Identity); first 8 chars are
#     enough to be recognizable in shell prompts and journal lines
#     without dragging the full 36-char UUID everywhere. The hosts-file
#     entry under 127.0.1.1 is the Debian convention and what
#     `hostname -f` resolves against.
vm_hostname="atlas-${VIRTUAL_MACHINE_NAME:0:8}"
echo "$vm_hostname" | sudo install -m 0644 /dev/stdin "${mount_point}/etc/hostname"
printf '\n127.0.1.1\t%s\n' "$vm_hostname" | \
    sudo tee -a "${mount_point}/etc/hosts" >/dev/null

# 2b. Swapfile. 512 MiB is enough to keep small apt installs from OOMing
#     on the 484-MiB default; users with bigger VMs can replace it.
#     Created inside the rootfs while mounted so it lands at /swapfile
#     on boot — the fstab from sync-image picks it up.
sudo dd if=/dev/zero of="${mount_point}/swapfile" bs=1M count=512 status=none
sudo chmod 0600 "${mount_point}/swapfile"
sudo mkswap "${mount_point}/swapfile" >/dev/null

# 2c. SSH host keys. sync-image.sh deletes the shared CI keys from the
#     image; we expected the guest to regenerate via `ssh-keygen -A` at
#     first boot, but the Firecracker CI rootfs has no first-boot
#     mechanism (no cloud-init, no ssh-keygen.service), so sshd dies
#     when keys are missing. Generate per-VM keys here on the host and
#     drop them in — each VM still gets unique keys, no first-boot
#     dependency. ssh-keygen accepts arbitrary output paths so we can
#     write directly into the mounted rootfs.
sudo install -d -m 0755 "${mount_point}/etc/ssh"
for key_type in rsa ecdsa ed25519; do
    key_path="${mount_point}/etc/ssh/ssh_host_${key_type}_key"
    sudo rm -f "${key_path}" "${key_path}.pub"
    sudo ssh-keygen -q -t "$key_type" -f "$key_path" -N "" -C "root@${vm_hostname}"
done

# 2d. machine-id. sync-image.sh truncated it to zero bytes expecting
#     systemd to regenerate at first boot — same caveat as 2c, the
#     stripped image has no reliable first-boot path. Write 32
#     lowercase hex chars at provision time (the format
#     systemd-machine-id-setup uses). Derived from the VM UUID so it's
#     stable across reboots of the same VM.
machine_id="$(printf '%s' "$VIRTUAL_MACHINE_NAME" | tr -d '-' | head -c 32)"
echo "$machine_id" | sudo install -m 0444 /dev/stdin "${mount_point}/etc/machine-id"

sudo umount "$mount_point"
sudo rmdir "$mount_point"
trap - EXIT

# 3. Firecracker config.
sudo install -m 0644 /dev/stdin "${vm_directory}/firecracker.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "${image_directory}/${KERNEL_FILENAME}",
    "boot_args": "console=ttyS0 reboot=k panic=1"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "${rootfs_path}",
      "is_root_device": true,
      "is_read_only": false
    }
  ],
  "network-interfaces": [
    {
      "iface_id": "eth0",
      "guest_mac": "${MAC_ADDRESS}",
      "host_dev_name": "${TAP_DEVICE}"
    }
  ],
  "machine-config": {
    "vcpu_count": ${VCPUS},
    "mem_size_mib": ${MEMORY_MB}
  }
}
EOF

# 4. Sidecar that vm-network-up.sh reads. Stable across host reboots.
sudo install -m 0644 /dev/stdin "${vm_directory}/network.env" <<EOF
TAP_DEVICE=${TAP_DEVICE}
VIRTUAL_MACHINE_IPV6=${VIRTUAL_MACHINE_IPV6}
EOF

# 5. Enable and start the systemd unit.
sudo systemctl enable --now "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service"

echo "Provisioned ${VIRTUAL_MACHINE_NAME}."
