#!/usr/bin/env bash
# Build an Ubuntu server cloud image for Metal.
set -euo pipefail

output=""
kernel_output=""
architecture=""
version=""
minimal=false
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
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

series_url="https://cloud-images.ubuntu.com/releases/$series"
rootfs_name="ubuntu-$version-server-cloudimg-amd64"
if $minimal; then
	series_url="https://cloud-images.ubuntu.com/minimal/releases/$series"
	rootfs_name="ubuntu-$version-minimal-cloudimg-amd64"
fi

prepare_build
build_rootfs
package_rootfs
