#!/bin/bash
# Phase 3 e2e: verify the bootstrap-installed pieces the firecracker-vm@ unit needs
# are in place. The per-VM hooks are `boat vm-*` verbs now (not durable .py), so
# check the boat binary the unit calls, the durable atlas package, and the unit.
set -euo pipefail

for path in \
    /usr/local/bin/boat \
    /var/lib/atlas/bin/atlas/lvm.py \
    /etc/systemd/system/firecracker-vm@.service; do
    test -f "$path"
    echo "$(basename "$path") OK"
done
