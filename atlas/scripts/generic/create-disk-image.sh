#!/usr/bin/env bash

set -euo pipefail

: "${DISK_IMAGE_PATH:?DISK_IMAGE_PATH is required}"
: "${DISK_IMAGE_SIZE_GIB:?DISK_IMAGE_SIZE_GIB is required}"

if [ -e "$DISK_IMAGE_PATH" ]; then
	echo "$DISK_IMAGE_PATH already exists" >&2
	exit 1
fi

# fallocate reserves the blocks, so the pool cannot run out of space under a full root file system.
mkdir -p "$(dirname "$DISK_IMAGE_PATH")"
fallocate --length "${DISK_IMAGE_SIZE_GIB}GiB" "$DISK_IMAGE_PATH"
chmod 600 "$DISK_IMAGE_PATH"
echo "Created $DISK_IMAGE_PATH with ${DISK_IMAGE_SIZE_GIB} GiB"
