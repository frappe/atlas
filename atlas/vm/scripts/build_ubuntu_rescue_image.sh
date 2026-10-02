#!/usr/bin/env bash
# Build the standalone Ubuntu rescue image for Metal.
set -euo pipefail

output=""
kernel_output=""
script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
step() { echo "==> $*"; }
source "$script_directory/ubuntu_image_functions.sh"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--output) output=$2; shift 2 ;;
		--kernel-output) kernel_output=$2; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[[ -n $output && -n $kernel_output ]] || { echo "--output and --kernel-output are required" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run this builder with root permissions" >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo "rescue builds require an amd64 build host" >&2; exit 2; }
for command in curl sha256sum unsquashfs mkfs.ext4 truncate zstd chroot python3; do
	command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done

image_path=$(realpath -m "$output")
kernel_path=$(realpath -m "$kernel_output")
work_path=$(mktemp -d)
trap 'rm -rf "$work_path"' EXIT
mkdir -p "$(dirname "$image_path")" "$(dirname "$kernel_path")"
rootfs_directory="$work_path/rootfs"
rootfs_path="$work_path/rootfs.squashfs"
rootfs_url="https://cloud-images.ubuntu.com/releases/noble/release-20260518/ubuntu-24.04-server-cloudimg-amd64.squashfs"
rootfs_sha256="bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74"

install_rescue_tools() {
	install -D -m 0755 "$script_directory/guest/rescue-reboot" \
		"$rootfs_directory/usr/lib/systemd/system-shutdown/atlas-rescue"
	install -D -m 0644 "$script_directory/guest/rescue-motd" "$rootfs_directory/etc/motd"
	# Cloud-init must not mount or resize the original VM disk.
	cat > "$rootfs_directory/etc/cloud/cloud.cfg.d/99-atlas-rescue.cfg" <<'EOF'
mounts: []
growpart:
  mode: "off"
resize_rootfs: false
EOF
	# Package scripts must not start daemons in the build host's namespaces.
	cat > "$rootfs_directory/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
	chmod 0755 "$rootfs_directory/usr/sbin/policy-rc.d"
	rm -f "$rootfs_directory/etc/resolv.conf"
	cp /etc/resolv.conf "$rootfs_directory/etc/resolv.conf"
	chroot "$rootfs_directory" env TMPDIR=/tmp apt-get update
	chroot "$rootfs_directory" env TMPDIR=/tmp DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		python3 e2fsprogs xfsprogs btrfs-progs util-linux parted lvm2 mdadm \
		cryptsetup-bin curl ca-certificates openssh-server vim-tiny linux-image-virtual
	chroot "$rootfs_directory" env TMPDIR=/tmp apt-get clean
	# Snap seeding can block cloud-final without Internet access.
	for unit in snapd.service snapd.socket snapd.seeded.service snapd.autoimport.service; do
		ln -sf /dev/null "$rootfs_directory/etc/systemd/system/$unit"
	done
	# The boot kernel must match the installed modules.
	local rescue_kernel
	rescue_kernel=$(find "$rootfs_directory/boot" -maxdepth 1 -name 'vmlinuz-*' | sort -V | tail -n1)
	[[ -n $rescue_kernel ]] || { echo "rescue kernel was not installed" >&2; return 1; }
	extract_vmlinux "$rescue_kernel" "$kernel_path.part"
	mv "$kernel_path.part" "$kernel_path"
	# Package installation can create host identities. Each guest must generate its own.
	rm -f "$rootfs_directory"/etc/ssh/ssh_host_* "$rootfs_directory/var/lib/dbus/machine-id"
	: > "$rootfs_directory/etc/machine-id"
	ln -s /etc/machine-id "$rootfs_directory/var/lib/dbus/machine-id"
	rm "$rootfs_directory/usr/sbin/policy-rc.d" "$rootfs_directory/etc/resolv.conf"
	ln -s ../run/systemd/resolve/stub-resolv.conf "$rootfs_directory/etc/resolv.conf"
	# Ubuntu ships the vsock transport as a module. Load it before shutdown begins.
	install -d -m 0755 "$rootfs_directory/etc/modules-load.d"
	printf 'vmw_vsock_virtio_transport\n' > "$rootfs_directory/etc/modules-load.d/atlas-rescue.conf"
}

fetch "$rootfs_url" "$rootfs_sha256" "$rootfs_path"
step "extract root file system"
unsquashfs -q -d "$rootfs_directory" "$rootfs_path"
install_cloud_init_datasource
install_guest_network
install_metadata_service
install_serial_console
install_ssh_metadata
install_rescue_tools
package_rootfs
