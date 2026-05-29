#!/bin/bash
# e2e: assert a VM is Paused by querying its Firecracker API socket.
# GET / returns InstanceInfo whose `state` is "Not started" | "Running" |
# "Paused" (firecracker swagger: describeInstance / InstanceInfo).
set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?}"

socket="/var/lib/atlas/run/${VIRTUAL_MACHINE_NAME}.sock"
state="$(sudo curl --fail --silent --unix-socket "$socket" http://localhost/ \
    | jq -r '.state // empty')"
if [ "$state" != "Paused" ]; then
    echo "expected Paused, API reports state=${state:-<none>}" >&2
    exit 1
fi
echo "paused"
