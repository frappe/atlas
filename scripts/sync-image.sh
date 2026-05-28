#!/bin/bash
# Download a kernel + rootfs pair into /var/lib/atlas/images/$IMAGE_NAME/.
# Convert the squashfs rootfs into a pristine ext4 of $DEFAULT_DISK_GB.
# Install the in-guest network unit so VMs come up with static IPv6.
# Idempotent: if files exist with matching checksums, exit early.
#
# Inputs (environment variables):
#   IMAGE_NAME         - directory name under /var/lib/atlas/images
#   KERNEL_URL         - HTTPS URL of the uncompressed vmlinux
#   KERNEL_FILENAME    - destination filename, e.g. vmlinux-6.1.141
#   KERNEL_SHA256      - hex digest of the kernel
#   ROOTFS_URL         - HTTPS URL of the source squashfs
#   ROOTFS_FILENAME    - destination ext4 filename, e.g. ubuntu-24.04.ext4
#   ROOTFS_SHA256      - hex digest of the *source squashfs*, not the ext4
#   DEFAULT_DISK_GB    - size of the pristine ext4
#   GUEST_NETWORK_UNIT - path on the server to the guest atlas-network.service
#                        (uploaded by the caller before running this script)

set -euo pipefail

: "${IMAGE_NAME:?required}"
: "${KERNEL_URL:?required}"
: "${KERNEL_FILENAME:?required}"
: "${KERNEL_SHA256:?required}"
: "${ROOTFS_URL:?required}"
: "${ROOTFS_FILENAME:?required}"
: "${ROOTFS_SHA256:?required}"
: "${DEFAULT_DISK_GB:?required}"
: "${GUEST_NETWORK_UNIT:?required}"

image_directory="/var/lib/atlas/images/${IMAGE_NAME}"
sudo install -d -m 0700 "$image_directory"

# 1. Kernel.
kernel_path="${image_directory}/${KERNEL_FILENAME}"
if [ -f "$kernel_path" ] && echo "${KERNEL_SHA256}  ${kernel_path}" | sudo sha256sum -c --status -; then
    echo "Kernel already present and matches checksum."
else
    sudo rm -f "${kernel_path}.part"
    sudo curl -fsSL --output "${kernel_path}.part" "$KERNEL_URL"
    echo "${KERNEL_SHA256}  ${kernel_path}.part" | sudo sha256sum -c -
    sudo mv "${kernel_path}.part" "$kernel_path"
fi

# 2. Rootfs.
rootfs_path="${image_directory}/${ROOTFS_FILENAME}"
if [ -f "$rootfs_path" ]; then
    echo "Rootfs already built. Skipping."
    exit 0
fi

squashfs_path="/tmp/atlas-${IMAGE_NAME}.squashfs"
extracted_directory="/tmp/atlas-${IMAGE_NAME}-rootfs"
sudo rm -f "${squashfs_path}.part" "$squashfs_path"
sudo rm -rf "$extracted_directory"

sudo curl -fsSL --output "${squashfs_path}.part" "$ROOTFS_URL"
echo "${ROOTFS_SHA256}  ${squashfs_path}.part" | sudo sha256sum -c -
sudo mv "${squashfs_path}.part" "$squashfs_path"

sudo unsquashfs -d "$extracted_directory" "$squashfs_path"

# 3. Install the guest network unit and a placeholder env file.
sudo install -d -m 0755 "${extracted_directory}/etc/systemd/system"
sudo install -d -m 0755 "${extracted_directory}/etc/systemd/system/multi-user.target.wants"
sudo install -m 0644 "$GUEST_NETWORK_UNIT" "${extracted_directory}/etc/systemd/system/atlas-network.service"
sudo ln -sf /etc/systemd/system/atlas-network.service \
    "${extracted_directory}/etc/systemd/system/multi-user.target.wants/atlas-network.service"
echo "" | sudo install -m 0644 /dev/stdin "${extracted_directory}/etc/atlas-network.env"

# 3a. Normalize the rootfs. The upstream Firecracker CI image ships with
#     leftovers that make a VM look like a half-built test artifact:
#     a bogus IPv4 derived from the MAC, identical host keys and
#     machine-id across all VMs, a /etc/hosts entry pointing at the
#     CI Docker container, root with a blank password, and motd
#     fragments that nag about unminimize. Strip them once here so
#     every VM that boots from this image looks like a fresh install.

# 3a.1 Kill fcnet — it derives a phantom IPv4/30 from the MAC at boot.
sudo rm -f "${extracted_directory}/usr/local/bin/fcnet-setup.sh"
sudo rm -f "${extracted_directory}/etc/systemd/system/fcnet.service"
sudo rm -f "${extracted_directory}/etc/systemd/system/sshd.service.wants/fcnet.service"
sudo rm -f "${extracted_directory}/etc/systemd/system/multi-user.target.wants/fcnet.service"

# 3a.2 Force regeneration of SSH host keys on first boot. Ubuntu's
#      ssh-keysign / ssh.service runs `ssh-keygen -A` if keys are
#      missing; deleting them here is the standard cloud-image trick.
sudo rm -f "${extracted_directory}/etc/ssh/ssh_host_"*_key \
           "${extracted_directory}/etc/ssh/ssh_host_"*_key.pub

# 3a.3 Force regeneration of machine-id on first boot. systemd
#      repopulates an empty /etc/machine-id at boot if it is zero
#      bytes (NOT if it is absent — absent triggers a different code
#      path that breaks journald).
sudo truncate -s 0 "${extracted_directory}/etc/machine-id"
sudo rm -f "${extracted_directory}/var/lib/dbus/machine-id"

# 3a.4 Clean /etc/hosts. The shipped file points 172.18.0.2 at the
#      Docker container the rootfs was built in; replace with a
#      minimal template. Per-VM hostname mapping is added at
#      provision time, not here.
sudo install -m 0644 /dev/stdin "${extracted_directory}/etc/hosts" <<'EOF'
127.0.0.1   localhost
::1         localhost ip6-localhost ip6-loopback
fe00::0     ip6-localnet
ff00::0     ip6-mcastprefix
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
EOF

# 3a.5 Lock root password (we are key-only by contract) and disable
#      SSH password auth defensively. sshd_config takes the *first*
#      value for each directive, so we prepend a small Atlas-managed
#      block at the top. Plain prepend (rather than sed-edit) survives
#      a stripped image that has no PasswordAuthentication line at all
#      and no `Include /etc/ssh/sshd_config.d/*.conf` near the top
#      (both real possibilities for the Firecracker CI rootfs).
sudo sed -i 's|^root:[^:]*:|root:!:|' "${extracted_directory}/etc/shadow"
sshd_config="${extracted_directory}/etc/ssh/sshd_config"
sudo tee "${sshd_config}.atlas" >/dev/null <<'EOF'
# Atlas-managed: enforce key-only SSH. First match wins in sshd_config,
# so these directives override anything later in the base file.
PasswordAuthentication no
PermitRootLogin prohibit-password

EOF
sudo sh -c "cat '${sshd_config}.atlas' '${sshd_config}' > '${sshd_config}.new'"
sudo mv "${sshd_config}.new" "$sshd_config"
sudo rm -f "${sshd_config}.atlas"

# 3a.6 Fix /home/ubuntu ownership (uid/gid 1000 from /etc/passwd).
#      The shipped image has it root-owned which breaks the user
#      it claims to provision.
sudo chown -R 1000:1000 "${extracted_directory}/home/ubuntu"

# 3a.7 Quieten the motd. 60-unminimize prints a "this image is
#      minimized" nag on every login; 50-motd-news fetches news
#      from Canonical which on v6-only with strict resolv.conf
#      hangs briefly.
sudo rm -f "${extracted_directory}/etc/update-motd.d/50-motd-news" \
           "${extracted_directory}/etc/update-motd.d/60-unminimize"

# 3a.8 Write a real /etc/fstab. The shipped one literally says
#      UNCONFIGURED. The rootfs UUID is unknown until mkfs runs
#      (step 4) and stable across copies, so we use the LABEL
#      mkfs sets in step 4 — see below.
sudo install -m 0644 /dev/stdin "${extracted_directory}/etc/fstab" <<'EOF'
LABEL=atlas-root  /  ext4  defaults,errors=remount-ro  0  1
/swapfile         none swap sw                         0  0
EOF

# 4. Build the ext4. Label `atlas-root` matches /etc/fstab.
sudo chown -R root:root "$extracted_directory"
sudo truncate -s "${DEFAULT_DISK_GB}G" "${rootfs_path}.part"
sudo mkfs.ext4 -q -L atlas-root -d "$extracted_directory" -F "${rootfs_path}.part"
sudo mv "${rootfs_path}.part" "$rootfs_path"

sudo rm -rf "$extracted_directory" "$squashfs_path"

echo "Image ${IMAGE_NAME} ready."
