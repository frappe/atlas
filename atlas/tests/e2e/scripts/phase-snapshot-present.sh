#!/bin/bash
# e2e: assert a snapshot rootfs file exists and is non-empty.
set -euo pipefail

: "${SNAPSHOT_ROOTFS_PATH:?}"

if ! sudo test -s "$SNAPSHOT_ROOTFS_PATH"; then
    echo "snapshot rootfs missing or empty: ${SNAPSHOT_ROOTFS_PATH}" >&2
    exit 1
fi
echo "present $(sudo stat -c %s "$SNAPSHOT_ROOTFS_PATH") bytes"
