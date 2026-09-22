#!/usr/bin/env bash

set -euo pipefail

# This script only reads the host. Atlas parses and checks the report.
: "${DISK_IMAGE_PATH:?DISK_IMAGE_PATH is required}"
. /etc/os-release

has_kvm=false
if [ -c /dev/kvm ] && grep -qE '\b(vmx|svm)\b' /proc/cpuinfo; then
	has_kvm=true
fi

memory_bytes=$(lsmem --bytes --summary=only | awk -F: '/Total online memory/ { gsub(/ /, "", $2); print $2 }')

image_directory=$(dirname "$DISK_IMAGE_PATH")
while [ ! -d "$image_directory" ]; do
	image_directory=$(dirname "$image_directory")
done
image_available_bytes=$(df --output=avail --block-size=1 "$image_directory" | tail -n 1 | tr -d ' ')

disk_image=null
if [ -e "$DISK_IMAGE_PATH" ]; then
	is_file=false
	[ -f "$DISK_IMAGE_PATH" ] && is_file=true
	image_type=$(blkid --probe --output value --match-tag TYPE "$DISK_IMAGE_PATH" 2>/dev/null || true)
	disk_image=$(printf '{"name":"%s","size":%s,"fstype":"%s","is_file":%s}' \
		"$DISK_IMAGE_PATH" "$(stat --format=%s "$DISK_IMAGE_PATH")" "$image_type" "$is_file")
fi

echo "===INSPECTION_START==="
printf '{"hostname":"%s","machine":"%s","os_id":"%s","os_version":"%s","cpu_count":%s,"memory_bytes":%s,"has_kvm":%s,"image_available_bytes":%s,"disk_image":%s,"addresses":%s,"default_routes":%s,"block_devices":%s}\n' \
	"$(hostname)" \
	"$(uname -m)" \
	"${ID:-}" \
	"${VERSION_ID:-}" \
	"$(nproc)" \
	"${memory_bytes:-0}" \
	"$has_kvm" \
	"$image_available_bytes" \
	"$disk_image" \
	"$(ip -json address show)" \
	"$(ip -json route show default)" \
	"$(lsblk --json --bytes --paths --output NAME,TYPE,SIZE,FSTYPE,PTTYPE,MOUNTPOINT,RO,RM)"
echo "===INSPECTION_END==="
