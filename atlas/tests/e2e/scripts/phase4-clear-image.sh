#!/bin/bash
# Phase 4 e2e: delete the cached image rootfs on the server so the next
# sync-image.py invocation re-runs the full download + normalize + mkfs
# pipeline. Used by the image-sync use case to make every e2e run a real
# regression test of sync-image.py, not just of its short-circuit.
set -euo pipefail

: "${IMAGE_NAME:?}"
: "${ROOTFS_FILENAME:?}"

sudo rm -f "/var/lib/atlas/images/${IMAGE_NAME}/${ROOTFS_FILENAME}"

# Also drop the base image LV so the next sync re-runs ThinPool.import_base_image's
# create path (not its idempotent no-op) — the e2e is a real regression of the
# LV import, not just of its short-circuit. This deliberately removes an
# atlas-image-* LV, so it lvremoves directly rather than via LogicalVolume.remove,
# whose guard (correctly) refuses base-image removal from VM/snapshot teardown.
lv_name="atlas-image-${IMAGE_NAME}"
if sudo lvs --noheadings "atlas/${lv_name}" >/dev/null 2>&1; then
    sudo lvremove -f "atlas/${lv_name}" >/dev/null
fi
