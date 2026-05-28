#!/bin/bash
# Phase 4 e2e: delete the cached image rootfs on the server so the next
# sync-image.sh invocation re-runs the full download + normalize + mkfs
# pipeline. Used by the image-sync use case to make every e2e run a real
# regression test of sync-image.sh, not just of its short-circuit.
set -euo pipefail

: "${IMAGE_NAME:?}"
: "${ROOTFS_FILENAME:?}"

sudo rm -f "/var/lib/atlas/images/${IMAGE_NAME}/${ROOTFS_FILENAME}"
