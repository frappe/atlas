#!/bin/bash
# e2e: SSH into a provisioned VM over its public IPv6 and assert the data disk is
# a formatted, mounted, writable ext4 volume at MOUNT_AT (the guest's /dev/vdb).
# The host-side proof that the second drive + mkfs + fstab LABEL=atlas-data line
# all landed and the guest brought the mount up at boot.
#
# Inputs:
#   VIRTUAL_MACHINE_IPV6  - destination address for the SSH probe.
#   SSH_PRIVATE_KEY       - private half of the key Atlas injected.
#   MOUNT_AT              - the data-disk mount point (e.g. /home).

set -euo pipefail
# Disable bash -x tracing: SSH_PRIVATE_KEY is in scope and any expansion would
# trace the key value into the captured Task stderr (same as phase5-guest-identity).
{ set +x; } 2>/dev/null

: "${VIRTUAL_MACHINE_IPV6:?}"
: "${SSH_PRIVATE_KEY:?}"
: "${MOUNT_AT:?}"

key_file="$(mktemp /tmp/atlas-data-probe-XXXXXX.key)"
trap 'rm -f "$key_file"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" >"$key_file"
chmod 0600 "$key_file"

# Wait for sshd in the guest (first boot may run ssh-keygen -A).
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

# One round trip, one clean failure point.
guest bash -s "$MOUNT_AT" <<'REMOTE'
set -euo pipefail
mount_at="$1"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# 1. MOUNT_AT is a mountpoint (the data disk is mounted, not just present).
mountpoint -q "$mount_at" || fail "${mount_at} is not a mountpoint"

# 2. It is ext4.
fstype="$(findmnt -no FSTYPE "$mount_at")"
[ "$fstype" = "ext4" ] || fail "${mount_at} fstype is '${fstype}', want ext4"

# 3. It is backed by the second drive /dev/vdb (root is /dev/vda).
source_dev="$(findmnt -no SOURCE "$mount_at")"
[ "$source_dev" = "/dev/vdb" ] || fail "${mount_at} source is '${source_dev}', want /dev/vdb"

# 4. It is writable: write, read back, remove a marker.
marker="${mount_at%/}/.atlas-e2e-datadisk"
marker_value="atlas-data-$$"
printf '%s\n' "$marker_value" >"$marker" || fail "cannot write to ${mount_at}"
read_back="$(cat "$marker")"
rm -f "$marker"
[ "$read_back" = "$marker_value" ] || fail "read-back mismatch on ${mount_at}: '${read_back}'"

echo "OK data disk ext4 mounted+writable at ${mount_at} (/dev/vdb)"
REMOTE
