#!/bin/bash
# e2e: assert a snapshot rootfs file is gone (after delete or terminate).
set -euo pipefail

: "${SNAPSHOT_ROOTFS_PATH:?}"

if sudo test -e "$SNAPSHOT_ROOTFS_PATH"; then
    echo "expected snapshot to be gone, still present: ${SNAPSHOT_ROOTFS_PATH}" >&2
    exit 1
fi
echo "gone"
