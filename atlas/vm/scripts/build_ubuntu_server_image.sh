#!/usr/bin/env bash
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

# Boot the newest kernel that Ubuntu supports for the release: the HWE kernel when the release has one.
case "$version" in
	22.04) series=jammy; kernel_package=linux-image-generic-hwe-22.04 ;;
	24.04) series=noble; kernel_package=linux-image-generic-hwe-24.04 ;;
	26.04) series=resolute; kernel_package=linux-image-generic ;;
	*) echo "unsupported version: $version" >&2; exit 2 ;;
esac

archive_url="https://archive.ubuntu.com/ubuntu"
series_url="https://cloud-images.ubuntu.com/releases/$series"
rootfs_name="ubuntu-$version-server-cloudimg-amd64"
if $minimal; then
	series_url="https://cloud-images.ubuntu.com/minimal/releases/$series"
	rootfs_name="ubuntu-$version-minimal-cloudimg-amd64"
fi

for command in curl sha256sum unsquashfs mkfs.ext4 truncate zstd zcat ar depmod; do
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

# Use the dated folder of the latest release, so a release published during the build cannot mix files.
fetch_rootfs() {
	local serial release_url checksum
	serial=$(curl -fsSL "$series_url/release/unpacked/build-info.txt" | sed -n 's/^serial=//p')
	release_url="$series_url/release-$serial"
	checksum=$(curl -fsSL "$release_url/SHA256SUMS" | awk -v file="*$rootfs_name.squashfs" '$2 == file { print $1 }')
	fetch "$release_url/$rootfs_name.squashfs" "$checksum" "$1"
}

package_field() {
	awk -v name="$1" -v field="$2:" '
		$1 == "Package:" { found = ($2 == name) }
		found && $1 == field { print $2 }' "$packages_path"
}

fetch_package() {
	local filename checksum
	filename=$(package_field "$1" Filename)
	checksum=$(package_field "$1" SHA256)
	fetch "$archive_url/$filename" "$checksum" "$2"
}

# Kernel image packages compress their data with zstd and modules packages do not. zstd -f passes plain data through.
extract_package() {
	local member
	member=$(ar t "$1" | grep '^data\.tar')
	mkdir -p "$2"
	ar p "$1" "$member" | zstd -dcf | tar -x --keep-directory-symlink -C "$2"
}

rootfs_path="$work_path/rootfs.squashfs"
rootfs_directory="$work_path/rootfs"
fetch_rootfs "$rootfs_path"
step "extract root file system"
unsquashfs -q -d "$rootfs_directory" "$rootfs_path"

packages_path="$work_path/Packages"
curl -fsSL "$archive_url/dists/$series-updates/main/binary-amd64/Packages.gz" | zcat > "$packages_path"
# The kernel metapackage depends first on the versioned image, such as linux-image-7.0.0-38-generic.
kernel_version=$(package_field "$kernel_package" Depends | sed -E 's/^linux-image-//; s/,$//')

fetch_package "linux-image-$kernel_version" "$work_path/kernel.deb"
extract_package "$work_path/kernel.deb" "$work_path/kernel"
step "extract uncompressed vmlinux"
extract_vmlinux "$work_path/kernel/boot/vmlinuz-$kernel_version" "$kernel_path.part" || {
	echo "could not extract an ELF vmlinux from the Ubuntu kernel" >&2
	exit 1
}
mv "$kernel_path.part" "$kernel_path"
step "extracted $(basename "$kernel_path")"

# Docker and BPF need modules that match the kernel, whatever the root file system ships.
fetch_package "linux-modules-$kernel_version" "$work_path/modules.deb"
extract_package "$work_path/modules.deb" "$rootfs_directory"
depmod -b "$rootfs_directory" "$kernel_version"

install_cloud_init_datasource
install_guest_network
install_metadata_service
install_serial_console
install_ssh_metadata

package_rootfs
