#!/bin/bash
# Phase 3 e2e: verify the bootstrap-uploaded files are in place.
set -euo pipefail

for path in \
    /var/lib/atlas/bin/vm-network-up.py \
    /var/lib/atlas/bin/vm-network-down.py \
    /var/lib/atlas/bin/vm-disk-up.py \
    /var/lib/atlas/bin/atlas/lvm.py \
    /etc/systemd/system/firecracker-vm@.service; do
    test -f "$path"
    echo "$(basename "$path") OK"
done
