#!/usr/bin/env bash
# Build the root file system and kernel for the Atlas control plane VM.
set -euo pipefail

output=""
kernel_output=""
disk_gib=24
address=""
prefix=24
gateway=""
authorized_key=""
rootfs_url=""
rootfs_sha256=""
kernel_url=""
kernel_sha256=""
hostname=atlas-vm

step() { echo "==> $*"; }
fail() { echo "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		--disk-gib) disk_gib=$2; shift 2 ;;
		--address) address=$2; shift 2 ;;
		--prefix) prefix=$2; shift 2 ;;
		--gateway) gateway=$2; shift 2 ;;
		--authorized-key) authorized_key=$2; shift 2 ;;
		--rootfs-url) rootfs_url=$2; shift 2 ;;
		--rootfs-sha256) rootfs_sha256=$2; shift 2 ;;
		--kernel-url) kernel_url=$2; shift 2 ;;
		--kernel-sha256) kernel_sha256=$2; shift 2 ;;
		*) fail "unknown argument: $1" ;;
	esac
done

[[ -n $output && -n $kernel_output && -n $address && -n $gateway && -n $authorized_key ]] ||
	fail "--output, --kernel-output, --address, --gateway, and --authorized-key are required"
[[ $EUID -eq 0 ]] || fail "run this builder with root permissions"
[[ -f $authorized_key ]] || fail "no such public key: $authorized_key"

for command_name in curl sha256sum unsquashfs mkfs.ext4 truncate zstd ssh-keygen; do
	command -v "$command_name" >/dev/null || fail "missing command: $command_name"
done

image_path=$(realpath -m "$output")
kernel_path=$(realpath -m "$kernel_output")
work_path=$(mktemp -d)
rootfs_directory="$work_path/rootfs"

cleanup() { rm -rf "$work_path"; }
trap cleanup EXIT

mkdir -p "$(dirname "$image_path")" "$(dirname "$kernel_path")"

fetch() {
	local url=$1 checksum=$2 path=$3
	step "download $url"
	curl -fL --progress-bar --output "$path" "$url"
	echo "$checksum  $path" | sha256sum --check --status
}

# Firecracker boots an uncompressed ELF vmlinux. The Ubuntu kernel ships as a
# bzImage that holds one zstd stream.
extract_vmlinux() {
	local image=$1 output_path=$2 zstd_magic offset
	zstd_magic=$(printf '\050\265\057\375')
	offset=$(grep -aboF -- "$zstd_magic" "$image" | head -n1 | cut -d: -f1)
	[[ -n $offset ]] || fail "no zstd stream in $image"

	tail -c "+$((offset + 1))" "$image" | zstd -cdq > "$output_path" 2>/dev/null || true
	[[ $(head -c 4 "$output_path" | od -An -tx1 | tr -d ' \n') == "7f454c46" ]] ||
		fail "the decompressed kernel is not an ELF vmlinux"
}

# cloud-init owns networking, the hostname, and the Secure Shell keys in the
# cloud image. This VM has no metadata service, so the image carries all three
# and cloud-init stays off.
install_network() {
	rm -f "$rootfs_directory/etc/resolv.conf"
	echo "nameserver 1.1.1.1" > "$rootfs_directory/etc/resolv.conf"

	install -d -m 0755 "$rootfs_directory/etc/systemd/network"
	cat > "$rootfs_directory/etc/systemd/network/10-atlas.network" <<NETWORK
[Match]
Name=eth0

[Network]
Address=$address/$prefix
Gateway=$gateway
DNS=1.1.1.1
NETWORK

	enable_unit systemd-networkd.service multi-user.target.wants
	ln -sf /dev/null "$rootfs_directory/etc/systemd/system/systemd-networkd-wait-online.service"
	touch "$rootfs_directory/etc/cloud/cloud-init.disabled"
}

install_identity() {
	echo "$hostname" > "$rootfs_directory/etc/hostname"
	cat > "$rootfs_directory/etc/hosts" <<HOSTS
127.0.0.1 localhost
127.0.1.1 $hostname
HOSTS
}

# Run one standalone sshd. Socket activation and the service are two owners of
# port 22, and only one of them can hold it.
install_secure_shell() {
	ssh-keygen -A -f "$rootfs_directory" >/dev/null
	install -d -m 0700 "$rootfs_directory/root/.ssh"
	install -m 0600 "$authorized_key" "$rootfs_directory/root/.ssh/authorized_keys"

	rm -f "$rootfs_directory/etc/systemd/system/sockets.target.wants/ssh.socket"
	enable_unit ssh.service multi-user.target.wants
	enable_unit serial-getty@ttyS0.service getty.target.wants
}

enable_unit() {
	local unit=$1 target_directory=$2 unit_file=$1
	[[ $unit == *@*.* ]] && unit_file="${unit%%@*}@.${unit##*.}"
	install -d -m 0755 "$rootfs_directory/etc/systemd/system/$target_directory"
	ln -sf "/lib/systemd/system/$unit_file" \
		"$rootfs_directory/etc/systemd/system/$target_directory/$unit"
}

fetch "$rootfs_url" "$rootfs_sha256" "$work_path/rootfs.squashfs"
fetch "$kernel_url" "$kernel_sha256" "$work_path/vmlinuz"

step "extract the kernel"
extract_vmlinux "$work_path/vmlinuz" "$kernel_path.part"
mv "$kernel_path.part" "$kernel_path"

step "extract the root file system"
unsquashfs -q -d "$rootfs_directory" "$work_path/rootfs.squashfs"

install_network
install_identity
install_secure_shell

step "create a ${disk_gib} GiB ext4 image"
truncate -s "${disk_gib}G" "$image_path.part"
mkfs.ext4 -q -F -L cloudimg-rootfs -d "$rootfs_directory" "$image_path.part"
mv "$image_path.part" "$image_path"

echo "Built $image_path"
echo "Built $kernel_path"
