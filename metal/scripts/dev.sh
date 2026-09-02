#!/usr/bin/env bash
# Prepare a throwaway host for metald. Run this before `metald serve`.
# Runtime files live under /tmp/metald. The config file lives under /var/lib/metal.
# Safe to run again. Requires root.
#
# Uses an Ubuntu *cloud* image with cloud-init. Each VM gets its Secure Shell key
# through the Firecracker metadata service and the guest initialization service.
set -euo pipefail

# Development paths. Keep external settings in the environment.
WORKDIR=/tmp/metald
BULK=${METALD_BULK_DIR:-$WORKDIR}
POOL=${METALD_POOL:-metal}
FC_VER=${METALD_FC_VERSION:-v1.10.1}
LISTEN=${METALD_LISTEN:-127.0.0.1:8080}
KERNEL_DIR=$WORKDIR/kernels
VAR_DIR=$WORKDIR/machines
BIN=$WORKDIR/bin
KEYDIR=$WORKDIR/keys
CONFIG=/var/lib/metal/metald.toml
ARCH=$(uname -m)
CI=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/$ARCH

# Bulk storage can use any file system. Point it at a large disk.
# Runtime files stay under WORKDIR because they need a POSIX file system.

[[ $EUID -eq 0 ]] || { echo "metald dev script: must run as root" >&2; exit 1; }

step() { echo "==> $*"; }

# fetch URL DEST downloads to a temporary file and renames it on success.
fetch() { [[ -f $2 ]] || { curl -fsSL -o "$2.part" "$1" && mv "$2.part" "$2"; }; }

# Create the directories used by the development host.
mkdir -p "$WORKDIR" "$BIN" "$KERNEL_DIR/ubuntu" "$BULK/images" "$KEYDIR" \
         "$VAR_DIR"

# Use half the free space for the pool, limited to 8G through 30G.
# Set METALD_POOL_SIZE to choose a different size.
avail_gb=$(( $(df -Pk "$BULK" | awk 'NR==2{print $4}') / 1024 / 1024 ))
sz=$(( avail_gb / 2 )); (( sz < 8 )) && sz=8; (( sz > 30 )) && sz=30
POOL_SIZE=${METALD_POOL_SIZE:-${sz}G}
(( avail_gb < 6 )) && echo "WARNING: only ${avail_gb}G free under $BULK; a cloud image needs ~4G." >&2

# Download Firecracker and Jailer when they are not present.
step "Firecracker and Jailer ($FC_VER)"
if [[ ! -x $BIN/firecracker || ! -x $BIN/jailer ]]; then
	curl -fsSL -o "$WORKDIR/fc.tgz" \
		"https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER/firecracker-$FC_VER-$ARCH.tgz"
	tar -xzf "$WORKDIR/fc.tgz" -C "$WORKDIR"
	cp "$WORKDIR/release-$FC_VER-$ARCH/firecracker-$FC_VER-$ARCH" "$BIN/firecracker"
	cp "$WORKDIR/release-$FC_VER-$ARCH/jailer-$FC_VER-$ARCH" "$BIN/jailer"
	chmod +x "$BIN/firecracker" "$BIN/jailer"
fi

# Download the guest kernel and write its boot arguments.
step "guest kernel"
fetch "$CI/vmlinux-5.10.223" "$KERNEL_DIR/ubuntu/vmlinux"
# The continuous integration root file system is partitionless ext4. The whole
# /dev/vda is the file system.
echo "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw" > "$KERNEL_DIR/ubuntu/boot-args"

# Download the root file system and add the guest key helper.
step "Ubuntu root file system and metadata service key helper"
rootfs=$BULK/images/ubuntu.ext4
fetch "$CI/ubuntu-22.04.ext4" "$rootfs"
# Add a keyless boot helper that pulls each VM key from the metadata service.
# Only the fetch is stored in the image. Skip it when the helper already exists.
mnt=$BULK/mnt; mkdir -p "$mnt"
mount -o loop "$rootfs" "$mnt"
if [[ ! -e "$mnt/usr/local/sbin/metal-sshkey" ]]; then
	install -Dm755 /dev/stdin "$mnt/usr/local/sbin/metal-sshkey" <<-'EOF'
		#!/bin/sh
		ip route add 169.254.169.254 dev eth0 2>/dev/null || true
		url=http://169.254.169.254/latest/meta-data/public-keys/0/openssh-key
		key=$(curl -fsS "$url" 2>/dev/null || wget -qO- "$url" 2>/dev/null || true)
		[ -n "$key" ] || exit 0
		mkdir -p /root/.ssh && chmod 700 /root/.ssh
		grep -qxF "$key" /root/.ssh/authorized_keys 2>/dev/null || echo "$key" >> /root/.ssh/authorized_keys
		chmod 600 /root/.ssh/authorized_keys
	EOF
	cat > "$mnt/etc/systemd/system/metal-sshkey.service" <<-'EOF'
		[Unit]
		Description=install Secure Shell key from the metadata service
		Before=ssh.service sshd.service
		[Service]
		Type=oneshot
		ExecStart=/usr/local/sbin/metal-sshkey
		RemainAfterExit=yes
		[Install]
		WantedBy=multi-user.target
	EOF
	mkdir -p "$mnt/etc/systemd/system/multi-user.target.wants"
	ln -sf ../metal-sshkey.service "$mnt/etc/systemd/system/multi-user.target.wants/metal-sshkey.service"
fi
umount "$mnt"

# Create the key used by the integration test.
step "Secure Shell key pair"
[[ -f $KEYDIR/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N "" -f "$KEYDIR/id_ed25519"

step "ZFS pool ($POOL_SIZE)"
# Use a file as the pool's virtual device.
img=$(realpath -m "$BULK")/pool.img
[[ -f $img ]] || truncate -s "$POOL_SIZE" "$img"
# Keep the pool unmounted because it only contains virtual block devices.
zpool list "$POOL" >/dev/null 2>&1 || zpool create -f -m none "$POOL" "$img"
# Create the parent data sets. Their virtual block devices still get /dev/zvol nodes.
zfs list "$POOL/images" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/images"
zfs list "$POOL/vms" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/vms"

# Create the base virtual block device and its clone snapshot.
step "base image ($POOL/images/ubuntu)"
if ! zfs list "$POOL/images/ubuntu" >/dev/null 2>&1; then
	bytes=$(stat -c %s "$rootfs")
	# Create a raw virtual block device with a 16K block size.
	zfs create -V "$((bytes / 1024 / 1024 + 64))M" -o volblocksize=16k "$POOL/images/ubuntu"
	udevadm settle  # wait for the virtual block device
	dd if="$rootfs" of="/dev/zvol/$POOL/images/ubuntu" bs=4M conv=sparse,fsync status=none
	# Create the read-only source for each VM clone.
	zfs snapshot "$POOL/images/ubuntu@ready"
fi

step "systemd template unit (metal-vm@.service)"
# Use development paths in the generated unit. The committed unit uses production paths.
cat > /etc/systemd/system/metal-vm@.service <<EOF
[Unit]
Description=metal microVM %i
After=network.target
[Service]
Type=exec
EnvironmentFile=$VAR_DIR/%i/jailer.env
ExecStart=$BIN/jailer \$JAILER_ARGS
Restart=no
EOF
systemctl daemon-reload

# Enable forwarding and network address translation for guest traffic.
step "forwarding and network address translation"
uplink=$(ip route show default | awk '{print $5; exit}')
sysctl -q -w net.ipv4.ip_forward=1
if [[ -n $uplink ]] && ! iptables -t nat -C POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE 2>/dev/null; then
	iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE
fi

# Write the configuration file used by metald.
step "config ($CONFIG)"
cat > "$CONFIG" <<EOF
[metald]
base_dir = "$WORKDIR"
listen   = "$LISTEN"

[firecracker]
binary_path = "$BIN/firecracker"
sockets_dir = "$WORKDIR/run"

[jailer]
binary_path = "$BIN/jailer"

[zfs]
pool = "$POOL"
EOF

step "ready: run metald serve"
step "key $KEYDIR/id_ed25519; ssh as user 'root'"
