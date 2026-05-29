#!/bin/bash
# Delete a VM disk snapshot's files. Idempotent: a missing path is a no-op.
# Run from Virtual Machine Snapshot.on_trash when the row is deleted.
#
# Inputs:
#   SNAPSHOT_ROOTFS_PATH  - absolute path to the snapshot rootfs to remove

set -euo pipefail

: "${SNAPSHOT_ROOTFS_PATH:?required}"

snapshot_directory="$(dirname "$SNAPSHOT_ROOTFS_PATH")"
sudo rm -rf "$snapshot_directory"

echo "Deleted snapshot ${snapshot_directory}."
