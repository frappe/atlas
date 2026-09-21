#!/usr/bin/env bash
# Grow the root file system or the storage pool into its resized disk. You can run this script again.

set -eu

: "${TARGET:?TARGET is required}"

storage_pool_name=${STORAGE_POOL_NAME:-metal}

grow_root() {
	local device_number partition source file_system_type disk number status

	# The mount source can be /dev/root, so find the partition by its device number.
	device_number=$(findmnt -no MAJ:MIN / | tr -d ' ')
	partition=$(basename "$(readlink -f "/sys/dev/block/$device_number")")
	source=/dev/$partition
	file_system_type=$(findmnt -no FSTYPE /)
	if [ "$file_system_type" != ext4 ]; then
		echo "the root file system is $file_system_type; this script grows ext4 only" >&2
		exit 1
	fi
	if ! command -v growpart >/dev/null; then
		echo "growpart is missing; install cloud-guest-utils" >&2
		exit 1
	fi

	disk=/dev/$(lsblk -no PKNAME "$source")
	number=$(cat "/sys/class/block/$partition/partition")

	echo "==> grow partition $number of $disk"
	# growpart exits 1 when the partition already fills the disk.
	status=0
	growpart "$disk" "$number" || status=$?
	if [ "$status" -gt 1 ]; then
		exit "$status"
	fi

	echo "==> grow the file system on $source"
	resize2fs "$source"
	df -h /
}

grow_storage() {
	: "${STORAGE_POOL_DEVICE:?STORAGE_POOL_DEVICE is required}"

	echo "==> grow pool $storage_pool_name onto $STORAGE_POOL_DEVICE"
	zpool online -e "$storage_pool_name" "$STORAGE_POOL_DEVICE"
	zpool list "$storage_pool_name"
}

case "$TARGET" in
root) grow_root ;;
storage) grow_storage ;;
*)
	echo "TARGET must be root or storage" >&2
	exit 1
	;;
esac
