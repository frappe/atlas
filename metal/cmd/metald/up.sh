#!/usr/bin/env bash
# Idempotent dev bootstrap for `metald up`. Everything lives under $METALD_WORKDIR
# (default /tmp/metald). Safe to re-run. Requires root.
#
# Uses an Ubuntu *cloud* image (ships cloud-init), so per-VM SSH keys arrive via
# MMDS: the API's ssh_keys -> firecracker MMDS -> cloud-init -> authorized_keys.
set -euo pipefail

WORKDIR=${METALD_WORKDIR:-/tmp/metald}
# Bulk storage (zfs pool image + image downloads) can live on any fs — point it
# at a big disk. The rest (chroot, kernels, keys, socket) needs a POSIX fs for
# mknod/hardlinks/perms, so it stays under WORKDIR.
BULK=${METALD_BULK_DIR:-$WORKDIR}
POOL=${METALD_POOL:-metal}
KERNEL_DIR=${METALD_KERNEL_DIR:-$WORKDIR/kernels}
BIN=$(dirname "${METALD_FIRECRACKER:-$WORKDIR/bin/firecracker}")
KEYDIR=$WORKDIR/keys
FC_VER=${METALD_FC_VERSION:-v1.10.1}
ARCH=$(uname -m)
CI=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/$ARCH

[[ $EUID -eq 0 ]] || { echo "metald up: must run as root" >&2; exit 1; }
step() { echo "==> $*"; }
# fetch URL DEST — atomic: download to .part, rename on success, so a partial
# download is never mistaken for a complete file on the next run.
fetch() { [[ -f $2 ]] || { curl -fsSL -o "$2.part" "$1" && mv "$2.part" "$2"; }; }
mkdir -p "$WORKDIR" "$BIN" "$KERNEL_DIR/ubuntu" "$BULK/images" "$KEYDIR" \
         "$METALD_CHROOT_BASE" "$METALD_VAR_DIR"

# Pool size: half the bulk dir's free space, clamped to [8G, 30G] (a cloud image
# base is ~3.5G; the rest is clone headroom). Override with METALD_POOL_SIZE.
avail_gb=$(( $(df -Pk "$BULK" | awk 'NR==2{print $4}') / 1024 / 1024 ))
sz=$(( avail_gb / 2 )); (( sz < 8 )) && sz=8; (( sz > 30 )) && sz=30
POOL_SIZE=${METALD_POOL_SIZE:-${sz}G}
(( avail_gb < 6 )) && echo "WARNING: only ${avail_gb}G free under $BULK; a cloud image needs ~4G." >&2

step "firecracker + jailer ($FC_VER)"
if [[ ! -x $BIN/firecracker || ! -x $BIN/jailer ]]; then
	curl -fsSL -o "$WORKDIR/fc.tgz" \
		"https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER/firecracker-$FC_VER-$ARCH.tgz"
	tar -xzf "$WORKDIR/fc.tgz" -C "$WORKDIR"
	cp "$WORKDIR/release-$FC_VER-$ARCH/firecracker-$FC_VER-$ARCH" "$BIN/firecracker"
	cp "$WORKDIR/release-$FC_VER-$ARCH/jailer-$FC_VER-$ARCH" "$BIN/jailer"
	chmod +x "$BIN/firecracker" "$BIN/jailer"
fi

step "kernel"
fetch "$CI/vmlinux-5.10.223" "$KERNEL_DIR/ubuntu/vmlinux"
# The CI rootfs is a partitionless ext4: the whole /dev/vda IS the filesystem.
echo "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw" > "$KERNEL_DIR/ubuntu/boot-args"

step "firecracker ubuntu rootfs + MMDS ssh-key shim"
rootfs=$BULK/images/ubuntu.ext4
fetch "$CI/ubuntu-22.04.ext4" "$rootfs"
# Bake a keyless boot shim that pulls the per-VM key from MMDS. The key always
# comes through the API (ssh_keys -> MMDS); only the fetch is baked in. Guarded
# on the shim's presence so a pre-staged rootfs still gets it.
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
		Description=install ssh key from MMDS
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

step "ssh keypair"
[[ -f $KEYDIR/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N "" -f "$KEYDIR/id_ed25519"

step "zfs pool ($POOL_SIZE)"
# A file vdev: zpool uses the image file directly, so no loop device is needed.
img=$(realpath -m "$BULK")/pool.img
[[ -f $img ]] || truncate -s "$POOL_SIZE" "$img"
# zpool create -f <pool> <file>: create the pool on the image; -f lets a plain
# file act as the vdev (zpool otherwise expects a whole disk/partition).
zpool list "$POOL" >/dev/null 2>&1 || zpool create -f "$POOL" "$img"

step "base-ubuntu image"
if ! zfs list "$POOL/base/ubuntu" >/dev/null 2>&1; then
	bytes=$(stat -c %s "$rootfs")
	# zfs create -V <size> -o volblocksize=16k: a zvol (raw block device) of the
	# given provisioned size, 16k record size (per-VM clones inherit it).
	zfs create -V "$((bytes / 1024 / 1024 + 64))M" -o volblocksize=16k "$POOL/base/ubuntu"
	udevadm settle  # wait for /dev/zvol/... to appear
	dd if="$rootfs" of="/dev/zvol/$POOL/base/ubuntu" bs=4M conv=sparse,fsync status=none
	# zfs snapshot <base>@ready: the read-only source every per-VM clone branches from.
	zfs snapshot "$POOL/base/ubuntu@ready"
fi

step "systemd template unit (metal-vm@.service)"
# Generated with the dev paths so metald's StartUnit resolves it. The committed
# deploy/metal-vm@.service uses production paths (/var/lib/metal, /usr/bin).
cat > /etc/systemd/system/metal-vm@.service <<EOF
[Unit]
Description=metal microVM %i
After=network.target
[Service]
Type=exec
EnvironmentFile=$METALD_VAR_DIR/%i/jailer.env
ExecStart=${METALD_JAILER:-/usr/bin/jailer} \$JAILER_ARGS
Restart=no
EOF
systemctl daemon-reload

step "forwarding + NAT"
uplink=$(ip route show default | awk '{print $5; exit}')
sysctl -q -w net.ipv4.ip_forward=1
if [[ -n $uplink ]] && ! iptables -t nat -C POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE 2>/dev/null; then
	iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE
fi

step "ready — API on ${METALD_LISTEN:-127.0.0.1:8080}; key $KEYDIR/id_ed25519; ssh as user 'root'"
