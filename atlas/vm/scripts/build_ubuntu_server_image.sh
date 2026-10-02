#!/usr/bin/env bash
# Build an Ubuntu server cloud image for Metal.
set -euo pipefail

output=""
kernel_output=""
architecture=""
version=""
minimal=false
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

step() { echo "==> $*"; }
source "$script_directory/ubuntu_image_functions.sh"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		--architecture) architecture=$2; shift 2 ;;
		--version) version=$2; shift 2 ;;
		--minimal) minimal=true; shift ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[[ -n $output && -n $kernel_output && -n $architecture && -n $version ]] || { echo "--output, --kernel-output, --architecture, and --version are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run this builder with root permissions" >&2; exit 1; }

case "$architecture" in
	amd64) ;;
	*) echo "unsupported architecture: $architecture" >&2; exit 2 ;;
esac

# Keep each release URL with its checksums.
case "$version" in
	22.04)
		if $minimal; then
			echo "minimal images are available only for Ubuntu 24.04" >&2
			exit 2
		fi
		release_url="https://cloud-images.ubuntu.com/releases/jammy/release-20260826"
		rootfs_url="$release_url/ubuntu-22.04-server-cloudimg-amd64.squashfs"
		rootfs_sha256="a4f612de4736d534a5617531eb7c1771b0b14878549c9d32191fd50e3077eb4f"
		kernel_url="https://cloud-images.ubuntu.com/releases/noble/release-20260518/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic"
		kernel_sha256="3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6"
		;;
	24.04)
		if $minimal; then
			release_url="https://cloud-images.ubuntu.com/minimal/releases/noble/release-20260521"
			rootfs_url="$release_url/ubuntu-24.04-minimal-cloudimg-amd64.squashfs"
			rootfs_sha256="a288f0bd499e1a747f86fda8ec9822dd99a4e3c0721d89ffd9dd57608ff21072"
			kernel_url="$release_url/unpacked/ubuntu-24.04-minimal-cloudimg-amd64-vmlinuz-generic"
		else
			release_url="https://cloud-images.ubuntu.com/releases/noble/release-20260518"
			rootfs_url="$release_url/ubuntu-24.04-server-cloudimg-amd64.squashfs"
			rootfs_sha256="bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74"
			kernel_url="$release_url/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic"
		fi
		kernel_sha256="3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6"
		;;
	*) echo "unsupported version: $version" >&2; exit 2 ;;
esac

for command in curl sha256sum unsquashfs mkfs.ext4 truncate zstd; do
	command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done

image_path=$(realpath -m "$output")
kernel_path=$(realpath -m "$kernel_output")
work_path=$(mktemp -d)

cleanup() {
	rm -rf "$work_path"
}
trap cleanup EXIT

mkdir -p "$(dirname "$image_path")"
mkdir -p "$(dirname "$kernel_path")"

rootfs_path="$work_path/rootfs.squashfs"
rootfs_directory="$work_path/rootfs"
fetch "$rootfs_url" "$rootfs_sha256" "$rootfs_path"

vmlinuz_path="$work_path/vmlinuz"
fetch "$kernel_url" "$kernel_sha256" "$vmlinuz_path"
step "extract uncompressed vmlinux"
extract_vmlinux "$vmlinuz_path" "$kernel_path.part" || {
	echo "could not extract an ELF vmlinux from the Ubuntu kernel" >&2
	exit 1
}
mv "$kernel_path.part" "$kernel_path"
step "extracted $(basename "$kernel_path")"

step "extract root file system"
unsquashfs -q -d "$rootfs_directory" "$rootfs_path"

install_cloud_init_datasource
install_guest_network
install_metadata_service
install_serial_console
install_ssh_metadata

package_rootfs
