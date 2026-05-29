# Sourced library — NOT a standalone Task. Lives in scripts/lib/ so the
# scripts_catalog (which lists scripts/*.sh top-level) never treats it as
# runnable. Uploaded next to its callers by script_uploads.py and sourced as
# "$(dirname "$0")/prepare-rootfs.sh".
#
# Holds the rootfs preparation shared by provision-vm.sh, rebuild-vm.sh and the
# clone path: lay down a per-VM rootfs from a source (pristine image or a
# snapshot copy), resize it, and inject per-VM identity (SSH key, network env,
# hostname, swap, fresh host keys, machine-id). Each VM still gets unique
# identity even when the source bytes came from another VM's snapshot, because
# the host keys and machine-id are rewritten here from this VM's UUID.

# atlas_copy_rootfs SOURCE DEST DISK_GB
#   Copy SOURCE to DEST and grow the filesystem to DISK_GB. No-op if DEST
#   already exists (idempotent re-run).
atlas_copy_rootfs() {
    local source_rootfs="$1" dest_rootfs="$2" disk_gb="$3"
    if [ -f "$dest_rootfs" ]; then
        return 0
    fi
    sudo cp "$source_rootfs" "${dest_rootfs}.part"
    sudo truncate -s "${disk_gb}G" "${dest_rootfs}.part"
    sudo e2fsck -fy "${dest_rootfs}.part" >/dev/null 2>&1 || true
    sudo resize2fs "${dest_rootfs}.part" >/dev/null
    sudo mv "${dest_rootfs}.part" "$dest_rootfs"
}

# atlas_inject_identity ROOTFS VM_NAME IPV6 SSH_PUBLIC_KEY IPV4_GUEST_CIDR IPV4_GATEWAY
#   Mount ROOTFS and write this VM's identity into it: authorized_keys, the
#   per-VM network env (IPv6 + the private IPv4 egress link), hostname + hosts
#   entry, a 512 MiB swapfile, fresh SSH host keys, and a UUID-derived
#   machine-id. Unmounts on return (and on error, via the trap the caller is
#   expected to leave to us). The v4 args are required so a rebuilt/cloned VM
#   never silently loses its egress config.
atlas_inject_identity() {
    local rootfs_path="$1" vm_name="$2" vm_ipv6="$3" ssh_public_key="$4"
    local vm_ipv4="$5" vm_ipv4_gateway="$6"
    local mount_point
    mount_point="$(sudo mktemp -d /tmp/atlas-mount-XXXXXX)"
    sudo mount -o loop "$rootfs_path" "$mount_point"
    trap 'sudo umount "$mount_point" 2>/dev/null || true; sudo rmdir "$mount_point" 2>/dev/null || true' EXIT

    sudo install -d -m 0700 "${mount_point}/root/.ssh"
    printf '%s\n' "$ssh_public_key" | sudo install -m 0600 /dev/stdin "${mount_point}/root/.ssh/authorized_keys"

    sudo install -m 0644 /dev/stdin "${mount_point}/etc/atlas-network.env" <<EOF
VIRTUAL_MACHINE_IPV6=${vm_ipv6}
VIRTUAL_MACHINE_IPV4=${vm_ipv4}
VIRTUAL_MACHINE_IPV4_GATEWAY=${vm_ipv4_gateway}
EOF

    # Per-VM hostname. First 8 chars of the stable UUID are enough to recognize
    # the VM in prompts and journal lines; the 127.0.1.1 entry is the Debian
    # convention `hostname -f` resolves against.
    local vm_hostname="atlas-${vm_name:0:8}"
    echo "$vm_hostname" | sudo install -m 0644 /dev/stdin "${mount_point}/etc/hostname"
    printf '\n127.0.1.1\t%s\n' "$vm_hostname" | \
        sudo tee -a "${mount_point}/etc/hosts" >/dev/null

    # Swapfile. 512 MiB keeps small apt installs from OOMing on the 484-MiB
    # default; lands at /swapfile, picked up by the fstab from sync-image.
    sudo dd if=/dev/zero of="${mount_point}/swapfile" bs=1M count=512 status=none
    sudo chmod 0600 "${mount_point}/swapfile"
    sudo mkswap "${mount_point}/swapfile" >/dev/null

    # Fresh SSH host keys. The CI rootfs has no first-boot keygen, so sshd dies
    # without keys; generate per-VM keys here. On a snapshot/clone source this
    # also overwrites the source VM's keys so the new VM is not a duplicate.
    sudo install -d -m 0755 "${mount_point}/etc/ssh"
    local key_type key_path
    for key_type in rsa ecdsa ed25519; do
        key_path="${mount_point}/etc/ssh/ssh_host_${key_type}_key"
        sudo rm -f "${key_path}" "${key_path}.pub"
        sudo ssh-keygen -q -t "$key_type" -f "$key_path" -N "" -C "root@${vm_hostname}"
    done

    # machine-id: 32 lowercase hex chars derived from the UUID (stable across
    # this VM's reboots, unique across VMs). Overwrites any value the source
    # rootfs carried.
    local machine_id
    machine_id="$(printf '%s' "$vm_name" | tr -d '-' | head -c 32)"
    echo "$machine_id" | sudo install -m 0444 /dev/stdin "${mount_point}/etc/machine-id"

    sudo umount "$mount_point"
    sudo rmdir "$mount_point"
    trap - EXIT
}
