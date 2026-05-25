#!/bin/bash
# Stop a VM. Networking teardown is fired by the unit's ExecStopPost.
#
# Inputs:
#   VIRTUAL_MACHINE_NAME

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?required}"

sudo systemctl stop "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service"
