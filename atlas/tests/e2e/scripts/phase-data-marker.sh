#!/bin/bash
# e2e: write or assert a marker file on the guest's data disk, over SSH. Used to
# prove the data disk round-trips through snapshot/restore and clone: write a
# marker, snapshot, overwrite it, restore, then assert the original came back.
#
# Inputs:
#   VIRTUAL_MACHINE_IPV6  - destination address for the SSH probe.
#   SSH_PRIVATE_KEY       - private half of the key Atlas injected.
#   MOUNT_AT              - the data-disk mount point (e.g. /home).
#   MODE                  - "write" (store MARKER) or "expect" (assert == MARKER).
#   MARKER                - the marker value.

set -euo pipefail
{ set +x; } 2>/dev/null  # keep SSH_PRIVATE_KEY out of the traced Task stderr

: "${VIRTUAL_MACHINE_IPV6:?}"
: "${SSH_PRIVATE_KEY:?}"
: "${MOUNT_AT:?}"
: "${MODE:?}"
: "${MARKER:?}"

key_file="$(mktemp /tmp/atlas-data-marker-XXXXXX.key)"
trap 'rm -f "$key_file"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" >"$key_file"
chmod 0600 "$key_file"

deadline=$((SECONDS + 90))
while ! ssh \
        -i "$key_file" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes \
        -o ConnectTimeout=5 \
        "root@${VIRTUAL_MACHINE_IPV6}" true 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "guest ssh not ready after 90s at ${VIRTUAL_MACHINE_IPV6}" >&2
        exit 1
    fi
    sleep 3
done

guest() {
    ssh \
        -i "$key_file" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "root@${VIRTUAL_MACHINE_IPV6}" "$@"
}

guest bash -s "$MOUNT_AT" "$MODE" "$MARKER" <<'REMOTE'
set -euo pipefail
mount_at="$1"; mode="$2"; marker="$3"
file="${mount_at%/}/.atlas-e2e-marker"

mountpoint -q "$mount_at" || { echo "FAIL: ${mount_at} is not a mountpoint" >&2; exit 1; }

case "$mode" in
    write)
        printf '%s\n' "$marker" >"$file"
        sync
        echo "OK wrote marker '${marker}' to ${file}"
        ;;
    expect)
        [ -f "$file" ] || { echo "FAIL: ${file} missing" >&2; exit 1; }
        actual="$(cat "$file")"
        [ "$actual" = "$marker" ] || { echo "FAIL: marker='${actual}' want='${marker}'" >&2; exit 1; }
        echo "OK marker '${marker}' present at ${file}"
        ;;
    *)
        echo "FAIL: unknown MODE '${mode}'" >&2; exit 1
        ;;
esac
REMOTE
