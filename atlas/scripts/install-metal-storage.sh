#!/usr/bin/env bash
# Create the Metal storage pool and datasets. Run this before anything writes under /var/lib/metal.

set -eu

: "${STORAGE_POOL_DEVICE:?STORAGE_POOL_DEVICE is required}"

storage_pool_name=${STORAGE_POOL_NAME:-metal}
base_dir=/var/lib/metal

if [ "$(id -u)" -ne 0 ]; then
	echo "install-metal-storage must run as root" >&2
	exit 1
fi

step() { echo "==> $*"; }
skip() { echo "    $* is already installed"; }


step "install zfs"
if command -v zpool >/dev/null; then
	skip "zfs"
else
	export DEBIAN_FRONTEND=noninteractive
	apt update -qq
	apt install -y -qq zfsutils-linux
fi


# is_empty_storage_pool_device checks the device immediately before ZFS can overwrite it.
is_empty_storage_pool_device() {
	if [ -f "$STORAGE_POOL_DEVICE" ]; then
		! blkid --probe "$STORAGE_POOL_DEVICE" >/dev/null 2>&1
		return
	fi

	[ -b "$STORAGE_POOL_DEVICE" ] || return 1
	# Scaleway gives a software RAID array, such as /dev/md2.
	case "$(lsblk --raw --noheadings --output TYPE "$STORAGE_POOL_DEVICE")" in
	disk | raid*) ;;
	*) return 1 ;;
	esac
	[ "$(lsblk --raw --noheadings --output NAME "$STORAGE_POOL_DEVICE" | wc -l)" -eq 1 ] || return 1
	[ -z "$(lsblk --raw --noheadings --output FSTYPE,PTTYPE,MOUNTPOINT "$STORAGE_POOL_DEVICE" | tr -d '[:space:]')" ] || return 1
	[ "$(lsblk --raw --noheadings --output RO "$STORAGE_POOL_DEVICE")" = 0 ] || return 1
	[ "$(lsblk --raw --noheadings --output RM "$STORAGE_POOL_DEVICE")" = 0 ] || return 1
	! blkid --probe "$STORAGE_POOL_DEVICE" >/dev/null 2>&1
}


step "zfs pool ($storage_pool_name)"
if zpool list "$storage_pool_name" >/dev/null 2>&1; then
	skip "pool $storage_pool_name"
else
	if ! is_empty_storage_pool_device; then
		echo "$STORAGE_POOL_DEVICE is not an empty storage pool device" >&2
		exit 1
	fi
	echo "    device: $STORAGE_POOL_DEVICE"
	zpool create -m none "$storage_pool_name" "$STORAGE_POOL_DEVICE"
fi
zfs list "$storage_pool_name/images" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/images"
zfs list "$storage_pool_name/vms" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/vms"
zfs list "$storage_pool_name/staging" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/staging"
zfs list "$storage_pool_name/rescue" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/rescue"
zfs list "$storage_pool_name/warm" >/dev/null 2>&1 || zfs create -o mountpoint=none "$storage_pool_name/warm"

# The root disk is small, so host state lives on the pool. An existing host
# moves its files to this dataset by hand before setup runs again.
if ! zfs list "$storage_pool_name/state" >/dev/null 2>&1; then
	if [ -n "$(ls -A "$base_dir" 2>/dev/null)" ]; then
		echo "$base_dir has files. Move them to $storage_pool_name/state, then run setup again" >&2
		exit 1
	fi
	zfs create -o mountpoint="$base_dir" "$storage_pool_name/state"
fi
if [ "$(zfs get -H -o value mounted "$storage_pool_name/state")" != yes ]; then
	zfs mount "$storage_pool_name/state"
fi
if [ "$(findmnt -n -o SOURCE --target "$base_dir")" != "$storage_pool_name/state" ]; then
	echo "$base_dir is not the mount point of $storage_pool_name/state" >&2
	exit 1
fi
