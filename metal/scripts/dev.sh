#!/usr/bin/env bash
# Prepare a disposable host for metald. Run as root before `metald serve`.
# Uses an Ubuntu cloud image and the Firecracker metadata service.
set -euo pipefail

# Keep external settings in the environment.
WORKDIR=/tmp/metald
BULK=${METALD_BULK_DIR:-$WORKDIR}
POOL=${METALD_POOL:-metal}
FC_VER=${METALD_FC_VERSION:-v1.10.1}
LISTEN=${METALD_LISTEN:-127.0.0.1:8080}
AUTH_TOKEN=${METALD_AUTH_TOKEN:-metal-development-token}
IMAGE_DIR=$WORKDIR/images
VAR_DIR=$WORKDIR/machines
BIN=$WORKDIR/bin
KEYDIR=$WORKDIR/keys
CONFIG=$WORKDIR/metald.toml
ARCH=$(uname -m)
CI=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/$ARCH

case $ARCH in
	x86_64) IMAGE_ARCHITECTURE=amd64 ;;
	aarch64) IMAGE_ARCHITECTURE=arm64 ;;
	*) echo "metald dev script: unsupported architecture $ARCH" >&2; exit 1 ;;
esac

# Bulk storage can use any file system. Runtime files need a POSIX file system.

[[ $EUID -eq 0 ]] || { echo "metald dev script: must run as root" >&2; exit 1; }

step() { echo "==> $*"; }

fetch() {
	[[ -f $2 ]] && return 0
	echo "    downloading $(basename "$2")"
	curl -fL --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2"
}

mkdir -p "$WORKDIR" "$BIN" "$IMAGE_DIR/ubuntu" "$BULK/downloads" "$KEYDIR" \
	"$VAR_DIR" "$(dirname "$CONFIG")"

# Use half the free space for the pool, limited to 8G through 30G.
# Set METALD_POOL_SIZE to choose a different size.
avail_gb=$(( $(df -Pk "$BULK" | awk 'NR==2{print $4}') / 1024 / 1024 ))
sz=$(( avail_gb / 2 )); (( sz < 8 )) && sz=8; (( sz > 30 )) && sz=30
POOL_SIZE=${METALD_POOL_SIZE:-${sz}G}
(( avail_gb < 6 )) && echo "WARNING: only ${avail_gb}G free under $BULK; a cloud image needs ~4G." >&2

step "Firecracker and Jailer ($FC_VER)"
if [[ ! -x $BIN/firecracker || ! -x $BIN/jailer ]]; then
	echo "    downloading firecracker-$FC_VER-$ARCH.tgz"
	curl -fL --progress-bar -o "$WORKDIR/fc.tgz" \
		"https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER/firecracker-$FC_VER-$ARCH.tgz"
	tar -xzf "$WORKDIR/fc.tgz" -C "$WORKDIR"
	cp "$WORKDIR/release-$FC_VER-$ARCH/firecracker-$FC_VER-$ARCH" "$BIN/firecracker"
	cp "$WORKDIR/release-$FC_VER-$ARCH/jailer-$FC_VER-$ARCH" "$BIN/jailer"
	chmod +x "$BIN/firecracker" "$BIN/jailer"
fi

step "guest kernel"
fetch "$CI/vmlinux-5.10.223" "$IMAGE_DIR/ubuntu/vmlinux"
# The image is a partitionless ext4 file system on /dev/vda.
echo "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw" > "$IMAGE_DIR/ubuntu/boot-args"

step "Ubuntu file system"
rootfs=$BULK/downloads/ubuntu.ext4
fetch "$CI/ubuntu-22.04.ext4" "$rootfs"
rootfs_sha256=$(sha256sum "$rootfs" | cut -d " " -f 1)
kernel_sha256=$(sha256sum "$IMAGE_DIR/ubuntu/vmlinux" | cut -d " " -f 1)

step "Secure Shell key pair"
[[ -f $KEYDIR/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N "" -f "$KEYDIR/id_ed25519"

step "ZFS pool ($POOL_SIZE)"
img=$(realpath -m "$BULK")/pool.img
[[ -f $img ]] || truncate -s "$POOL_SIZE" "$img"
zpool list "$POOL" >/dev/null 2>&1 || zpool create -f -m none "$POOL" "$img"
zfs list "$POOL/images" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/images"
zfs list "$POOL/vms" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/vms"
zfs list "$POOL/staging" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/staging"
zfs list "$POOL/warm" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/warm"

step "base image ($POOL/images/ubuntu)"
if ! zfs list "$POOL/images/ubuntu" >/dev/null 2>&1; then
	bytes=$(stat -c %s "$rootfs")
	zfs create -V "$((bytes / 1024 / 1024 + 64))M" -o volblocksize=16k "$POOL/images/ubuntu"
	udevadm settle
	dd if="$rootfs" of="/dev/zvol/$POOL/images/ubuntu" bs=4M conv=sparse,fsync status=none
	zfs snapshot "$POOL/images/ubuntu@ready"
fi

cat > "$IMAGE_DIR/ubuntu/manifest.json" <<EOF
{"rootfs_sha256":"$rootfs_sha256","kernel_sha256":"$kernel_sha256","architecture":"$IMAGE_ARCHITECTURE"}
EOF

step "systemd template unit (metal-vm@.service)"
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

step "forwarding and network address translation"
uplink=$(ip route show default | awk '{print $5; exit}')
sysctl -q -w net.ipv4.ip_forward=1
if [[ -n $uplink ]] && ! iptables -t nat -C POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE 2>/dev/null; then
	iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE
fi

step "config ($CONFIG)"
AUTH_TOKEN_HASH=$(printf %s "$AUTH_TOKEN" | sha256sum | cut -d " " -f 1)
cat > "$CONFIG" <<EOF
[metald]
base_dir = "$WORKDIR"
listen   = "$LISTEN"
auth_token_hash = "$AUTH_TOKEN_HASH"

[firecracker]
binary_path = "$BIN/firecracker"
sockets_dir = "$WORKDIR/run"

[jailer]
binary_path = "$BIN/jailer"

[zfs]
pool = "$POOL"
EOF

step "ready: run metald serve --config $CONFIG"
step "API token: $AUTH_TOKEN"
step "key $KEYDIR/id_ed25519; ssh as user 'root'"
