#!/usr/bin/env bash
# Prepare a disposable host for metald. Run as root before `metald serve`.
# Uses an Ubuntu cloud image and the Firecracker metadata service.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
IMAGE_BUILDER=$SCRIPT_DIR/../../atlas/vm/scripts/build_ubuntu_server_image.sh

# Keep external settings in the environment.
WORKDIR=${METALD_WORKDIR:-/tmp/metald}
BULK=${METALD_BULK_DIR:-$WORKDIR}
POOL=${METALD_POOL:-metal}
FC_VER=${METALD_FC_VERSION:-v1.16.1}
LISTEN=${METALD_LISTEN:-127.0.0.1:8080}
COORDINATION_LISTEN=${METALD_COORDINATION_LISTEN:-127.0.0.1:9001}
ATLAS_COMMON_NAME=${METALD_ATLAS_COMMON_NAME:-atlas.metal.test}
IMAGE_DIR=$WORKDIR/images
VAR_DIR=$WORKDIR/machines
BIN=$WORKDIR/bin
KEYDIR=$WORKDIR/keys
CONFIG=$WORKDIR/metald.toml
TLS_DIR=$WORKDIR/tls
ARCH=$(uname -m)

case $ARCH in
	x86_64) IMAGE_ARCHITECTURE=amd64 ;;
	aarch64) IMAGE_ARCHITECTURE=arm64 ;;
	*) echo "metald dev script: unsupported architecture $ARCH" >&2; exit 1 ;;
esac

# Bulk storage can use any file system. Runtime files need a POSIX file system.

[[ $EUID -eq 0 ]] || { echo "metald dev script: must run as root" >&2; exit 1; }

step() { echo "==> $*"; }

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

step "Atlas guest image (ssh key command, cloud-init datasource, metadata service)"
rootfs=$BULK/downloads/ubuntu.ext4
IMAGE_VERSION=${METALD_IMAGE_VERSION:-22.04}
# The builder creates the guest image when it is absent.
if [[ ! -f $rootfs ]]; then
	"$IMAGE_BUILDER" --output "$rootfs" --kernel-output "$BULK/downloads/ubuntu-vmlinux" \
		--platform "$IMAGE_ARCHITECTURE" --version "$IMAGE_VERSION"
fi

step "guest kernel (firecracker CI build)"
# Boot the Firecracker CI kernel. The Ubuntu kernel does not support the
# Firecracker device model; this kernel includes virtio and ext4 without an initramfs.
kernel=$IMAGE_DIR/ubuntu/vmlinux
if [[ ! -f $kernel ]]; then
	echo "    downloading vmlinux-5.10.223"
	curl -fL --progress-bar -o "$kernel.part" \
		"https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/$ARCH/vmlinux-5.10.223"
	mv "$kernel.part" "$kernel"
fi
# The image is a partitionless ext4 file system on /dev/vda.
echo "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw" > "$IMAGE_DIR/ubuntu/boot-args"
rootfs_sha256=$(sha256sum "$rootfs" | cut -d " " -f 1)
kernel_sha256=$(sha256sum "$kernel" | cut -d " " -f 1)

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
StandardInput=tty-force
StandardOutput=tty
StandardError=journal
TTYPath=$WORKDIR/run/consoles/%i
TTYReset=yes
TTYVHangup=yes
Restart=no
IOAccounting=yes
EOF
systemctl daemon-reload

step "forwarding and network address translation"
uplink=$(ip route show default | awk '{print $5; exit}')
sysctl -q -w net.ipv4.ip_forward=1
if [[ -n $uplink ]] && ! iptables -t nat -C POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE 2>/dev/null; then
	iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o "$uplink" -j MASQUERADE
fi

step "development certificates ($TLS_DIR)"
install -d -m 0700 "$TLS_DIR"
if [[ ! -s $TLS_DIR/ca.crt ]]; then
	openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 \
		-subj "/CN=Atlas Metal CA development" \
		-addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
		-addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign" \
		-keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" 2>/dev/null
fi

# issue_certificate writes one leaf that the development CA signed.
issue_certificate() {
	local name=$1 common_name=$2 extensions=$3

	openssl req -newkey rsa:2048 -nodes -subj "/CN=$common_name" \
		-keyout "$TLS_DIR/$name.key" -out "$TLS_DIR/$name.csr" 2>/dev/null
	openssl x509 -req -in "$TLS_DIR/$name.csr" -days 825 -sha256 \
		-CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
		-extfile <(printf '%s\n' "$extensions") \
		-out "$TLS_DIR/$name.crt" 2>/dev/null
	rm -f "$TLS_DIR/$name.csr"
}

listen_host=${LISTEN%:*}
issue_certificate node "metal-development" \
	"basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:metal-development,IP:$listen_host,IP:${COORDINATION_LISTEN%:*}"
issue_certificate atlas "$ATLAS_COMMON_NAME" \
	"basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth"
chmod 0600 "$TLS_DIR"/*.crt "$TLS_DIR"/*.key

step "config ($CONFIG)"
cat > "$CONFIG" <<EOF
[metald]
base_dir = "$WORKDIR"
listen   = "$LISTEN"
coordination_listen = "$COORDINATION_LISTEN"

[tls]
ca_file = "$TLS_DIR/ca.crt"
certificate_file = "$TLS_DIR/node.crt"
private_key_file = "$TLS_DIR/node.key"
atlas_common_name = "$ATLAS_COMMON_NAME"

[firecracker]
binary_path = "$BIN/firecracker"
sockets_dir = "$WORKDIR/run"

[jailer]
binary_path = "$BIN/jailer"

[zfs]
pool = "$POOL"

[wg_mesh]
uplink  = "$uplink"

EOF

step "ready: run metald serve --config $CONFIG"
step "call the API with --cacert $TLS_DIR/ca.crt --cert $TLS_DIR/atlas.crt --key $TLS_DIR/atlas.key"
step "key $KEYDIR/id_ed25519; ssh as user 'root'"
