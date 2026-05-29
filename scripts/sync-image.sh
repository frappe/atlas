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

# 1. Kernel. The Ubuntu cloud image ships a packed, zstd-compressed bzImage
#    (`vmlinuz`); Firecracker needs an uncompressed ELF `vmlinux`. We download
#    the packed file, verify it against KERNEL_SHA256 (the digest of the
#    *packed* artifact, from upstream SHA256SUMS), then decompress the zstd
#    payload to the final vmlinux. The extracted kernel is a derived artifact,
#    not separately checksummed — verifying the download is the integrity gate.
kernel_path="${image_directory}/${KERNEL_FILENAME}"
if [ -f "$kernel_path" ]; then
    echo "Kernel already present. Skipping."
else
    packed_path="${kernel_path}.vmlinuz"
    sudo rm -f "${packed_path}.part" "$packed_path"
    sudo curl -fsSL --output "${packed_path}.part" "$KERNEL_URL"
    echo "${KERNEL_SHA256}  ${packed_path}.part" | sudo sha256sum -c -
    sudo mv "${packed_path}.part" "$packed_path"

    # Decompress the embedded vmlinux. The Ubuntu kernel is a PE/EFI bzImage
    # whose payload is a zstd frame followed by a 4-byte size trailer, so plain
    # `unzstd`/`zstd -d` reject it ("unsupported format" — trailing bytes after
    # the frame). `zstd -dc -f` decompresses the valid frame and ignores the
    # trailer. We can't use the kernel.org extract-vmlinux helper: it verifies
    # with `readelf`, absent on a stock Firecracker host (it silently yields a
    # 0-byte file). So: locate the zstd magic (28 b5 2f fd), decompress from
    # there with `-f`, and confirm the ELF magic (7f 45 4c 46). `xxd | grep -bo`
    # gives a hex-nibble offset (byte = /2); `tail -c +N` is 1-indexed (+1).
    hex_offset="$(xxd -p "$packed_path" | tr -d '\n' | grep -bo '28b52ffd' | head -1 | cut -d: -f1)"
    if [ -z "$hex_offset" ]; then
        echo "No zstd magic in kernel image ${packed_path}" >&2
        exit 1
    fi
    byte_offset=$(( hex_offset / 2 ))
    sudo sh -c "tail -c +$((byte_offset + 1)) '${packed_path}' | zstd -dc -f > '${kernel_path}.part'"
    if [ "$(head -c 4 "${kernel_path}.part" | xxd -p)" != "7f454c46" ]; then
        echo "Decompressed kernel is not ELF" >&2
        sudo rm -f "${kernel_path}.part"
        exit 1
    fi
    sudo mv "${kernel_path}.part" "$kernel_path"
    sudo rm -f "$packed_path"
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

# 3a. Normalize the rootfs. The Ubuntu cloud image is built for a generic
#     cloud, not for Atlas's static-IPv6 / no-first-boot-agent model. Left
#     untouched it (a) blocks boot forever on cloud-init's datasource probe,
#     systemd-networkd-wait-online, and snapd seeding; (b) ships identical
#     host keys / machine-id across VMs; (c) trusts cloud-init for identity
#     Atlas instead injects at mount time. Strip/neutralize all of that once
#     here so every VM boots straight to a clean login.
#
#     NOTE (regression checklist): each step below must be a no-op OR a
#     correct strip on the current upstream image. If a step's target is
#     absent on a future image, that step becomes a documented no-op (the
#     `rm -f` / mask calls stay harmless); it is not silently dropped.

# 3a.1 Kill fcnet. This is a Firecracker-CI artifact (phantom IPv4/30 from the
#      MAC); the Ubuntu cloud image has none of these files, so every line is a
#      no-op today. Kept because `rm -f` is harmless and documents the contract.
sudo rm -f "${extracted_directory}/usr/local/bin/fcnet-setup.sh"
sudo rm -f "${extracted_directory}/etc/systemd/system/fcnet.service"
sudo rm -f "${extracted_directory}/etc/systemd/system/sshd.service.wants/fcnet.service"
sudo rm -f "${extracted_directory}/etc/systemd/system/multi-user.target.wants/fcnet.service"

# 3a.1b Neutralize cloud-init and the boot-blocking services. The cloud image
#       boots into cloud-init + systemd-networkd-wait-online + snapd seeding,
#       all of which hang indefinitely under Atlas (no datasource, static v6
#       brought up by atlas-network.service, no need for snap). Without this the
#       guest never reaches a login prompt (the e2e guest-identity probe is the
#       regression guard). We mask the units (symlink to /dev/null) so they
#       cannot start, and set cloud-init's own disable flag for good measure.
#       Masking is idempotent and survives even if the package is reinstalled.
sudo install -d -m 0755 "${extracted_directory}/etc/cloud"
sudo touch "${extracted_directory}/etc/cloud/cloud-init.disabled"
for unit in \
    cloud-init.service cloud-init-local.service cloud-config.service \
    cloud-final.service systemd-networkd-wait-online.service \
    snapd.seeded.service snapd.service snapd.socket; do
    sudo ln -sf /dev/null "${extracted_directory}/etc/systemd/system/${unit}"
done

# 3a.2 Strip the shipped SSH host keys so every VM doesn't share one
#      identity. We do NOT rely on first-boot regeneration (cloud-init is
#      masked, and we don't trust ssh.service keygen); provision-vm.sh writes
#      fresh per-VM host keys into the mounted rootfs at provision time.
sudo rm -f "${extracted_directory}/etc/ssh/ssh_host_"*_key \
           "${extracted_directory}/etc/ssh/ssh_host_"*_key.pub

# 3a.3 Force regeneration of machine-id on first boot. systemd
#      repopulates an empty /etc/machine-id at boot if it is zero
#      bytes (NOT if it is absent — absent triggers a different code
#      path that breaks journald).
sudo truncate -s 0 "${extracted_directory}/etc/machine-id"
sudo rm -f "${extracted_directory}/var/lib/dbus/machine-id"

# 3a.4 Normalize /etc/hosts to a minimal template. Per-VM hostname mapping
#      (the 127.0.1.1 line) is added at provision time, not here. Overwriting
#      is correct regardless of what the upstream file contains — Atlas owns it.
sudo install -m 0644 /dev/stdin "${extracted_directory}/etc/hosts" <<'EOF'
127.0.0.1   localhost
::1         localhost ip6-localhost ip6-loopback
fe00::0     ip6-localnet
ff00::0     ip6-mcastprefix
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
EOF

# 3a.5 Lock root password (key-only by contract) and enforce key-only SSH.
#      The Ubuntu cloud image's sshd_config has `Include
#      /etc/ssh/sshd_config.d/*.conf` near the top and ships
#      `60-cloudimg-settings.conf` enabling PasswordAuthentication. A prepend
#      to sshd_config would be overridden by that Include, so we drop our own
#      `00-atlas.conf` into the same directory — it sorts first, and first
#      match wins per directive, so it beats 60-cloudimg-settings.conf.
sudo sed -i 's|^root:[^:]*:|root:!:|' "${extracted_directory}/etc/shadow"
sudo install -d -m 0755 "${extracted_directory}/etc/ssh/sshd_config.d"
sudo install -m 0644 /dev/stdin \
    "${extracted_directory}/etc/ssh/sshd_config.d/00-atlas.conf" <<'EOF'
# Atlas-managed: enforce key-only SSH. Sorts before 60-cloudimg-settings.conf;
# first match wins per directive, so these take effect.
PasswordAuthentication no
PermitRootLogin prohibit-password
EOF

# 3a.6 Ensure /home/ubuntu is owned by uid/gid 1000 *if it exists*. The
#      Ubuntu cloud image does NOT ship /home/ubuntu — cloud-init creates the
#      `ubuntu` user on first boot, and we've masked cloud-init. Atlas SSHes
#      in as root (key injected at provision time), so the ubuntu user is
#      irrelevant to us; this is a guarded no-op on the cloud image, kept so a
#      future image that does ship the dir gets correct ownership.
if [ -d "${extracted_directory}/home/ubuntu" ]; then
    sudo chown -R 1000:1000 "${extracted_directory}/home/ubuntu"
fi

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
